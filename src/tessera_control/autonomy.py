"""Event-sourced bounded sessions over existing capability/operation authority.

No external effects, new grants, credential resolution or sandbox activation.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta

from .autonomy_models import (
    ActionUpdate,
    AutonomyPlan,
    AutonomyReceipt,
    AutonomySession,
    SessionCommand,
    StartAction,
)
from .capabilities import CapabilityStore
from .store import Conflict, Forbidden, NotFound, _connect, _parse_utc, canonical_digest

TERMINAL = {"cancelled", "unknown", "completed", "failed", "expired"}


def operation_session(connection, org_id, operation_id):
    row = connection.execute(
        "SELECT payload_json FROM events WHERE org_id=? AND event_type='autonomy.action_bound' "
        "AND json_extract(payload_json,'$.operation_id')=? LIMIT 1",
        (org_id, operation_id),
    ).fetchone()
    return json.loads(row[0])["session_id"] if row else None


def require_session_authority(store, connection, worker, operation_id):
    """Additional fence for bound operations, never authority for unbound ones."""
    ident = operation_session(connection, worker["org_id"], operation_id)
    if ident is None:
        return
    sessions = AutonomyStore(store.database_path.parent)
    state = sessions._load(connection, worker, ident)
    sessions._current(connection, worker, state)
    if (
        state["state"] != "running"
        or state["actions"][-1]["operation_id"] != operation_id
        or state["actions"][-1]["worker_id"] != worker["id"]
    ):
        raise Conflict("Autonomy session fenced; reconciliation only.")


class AutonomyStore(CapabilityStore):
    def _load(self, db, actor, ident):
        row = db.execute(
            "SELECT payload_json FROM events WHERE org_id=? AND event_type='autonomy.state' "
            "AND json_extract(payload_json,'$.id')=? ORDER BY cursor DESC LIMIT 1",
            (actor["org_id"], ident),
        ).fetchone()
        if not row:
            raise NotFound("Autonomy session not found.")
        state = json.loads(row[0])
        self._project(db, actor, state["project_id"])
        return state

    def _save(self, db, actor, state):
        state = AutonomySession.model_validate(state).model_dump(mode="json")
        self._event(
            db,
            actor,
            state["project_id"],
            state["task_id"],
            "autonomy.state",
            state["revision"],
            state,
        )
        return state

    def _basis(self, db, actor, task_id, plan):
        task = self._task(db, actor, task_id)
        policy = self._policy(db, actor, plan["capability"]["policy_id"])
        if (
            task["current_plan_digest"] != plan["task_plan_digest"]
            or task["revision"] - (task["status"] == "approved") != plan["task_plan_revision"]
            or policy["task_id"] != task_id
            or policy["plan_digest"] != plan["task_plan_digest"]
            or policy["plan_revision"] != plan["task_plan_revision"]
            or policy["declaration_digest"] != plan["capability"]["declaration_digest"]
        ):
            raise Conflict("Autonomy plan/capability basis changed.")
        declaration = json.loads(policy["declaration_json"])
        declared = {a["id"]: a for a in declaration["actions"]}
        for item in plan["acquisitions"]:
            if (
                item["action_id"] not in declared
                or declared[item["action_id"]]["kind"] != item["kind"]
            ):
                raise Conflict("Acquisition requires an exact declared capability action.")
        return policy, declaration

    @staticmethod
    def _effective(state):
        now = datetime.now(UTC)
        if state["state"] in TERMINAL:
            return state["state"]
        if state["state"] == "running" and (
            not state["lease_until"] or _parse_utc(state["lease_until"]) <= now
        ):
            return "unknown"
        if state["deadline"] and _parse_utc(state["deadline"]) <= now:
            return "unknown" if state["state"] == "running" else "expired"
        return state["state"]

    def _current(self, db, actor, state):
        if self._effective(state) in TERMINAL or not state["approved_by"]:
            raise Conflict("Autonomy session expired, unresolved or unapproved.")
        policy, declaration = self._basis(db, actor, state["task_id"], state["plan"])
        self._current_policy(db, actor, policy)
        reviewer = self._live_principal(db, {"id": state["approved_by"], "org_id": actor["org_id"]})
        self._human(reviewer)
        self._project(db, reviewer, state["project_id"])
        return declaration

    @staticmethod
    def _policy_reasons(plan, declaration):
        reasons = list(plan["capability"]["escalation"])
        if declaration["credential_refs"] or any(
            a["credential_ref"] for a in declaration["actions"]
        ):
            reasons.append("credentials")
        if any(a["access"] != "public" for a in plan["acquisitions"]):
            reasons.append("source_review_required")
        if (
            declaration["max_bytes"]
            > min(16 * 1024**2, plan["limits"]["storage_bytes"], plan["limits"]["output_bytes"])
            or len(declaration["actions"]) > plan["limits"]["max_actions"]
        ):
            reasons.append("preparation_caps_exceeded")
        return sorted(set(reasons))

    def create(self, actor, task_id, key, payload):
        plan = self._body(AutonomyPlan, payload)

        def command(db, live):
            policy, _ = self._basis(db, live, task_id, plan)
            count = db.execute(
                "SELECT count(*) FROM events WHERE org_id=? AND task_id=? "
                "AND event_type='autonomy.state' AND resource_revision=1",
                (live["org_id"], task_id),
            ).fetchone()[0]
            if count >= 32:
                raise Conflict("Task autonomy session limit reached.")
            ident = "autonomy_" + uuid.uuid4().hex
            state = AutonomySession(
                id=ident,
                org_id=live["org_id"],
                project_id=policy["project_id"],
                task_id=task_id,
                revision=1,
                plan=plan,
                plan_digest=canonical_digest(plan),
                workspace_id=canonical_digest([live["org_id"], policy["project_id"], ident]),
                state="proposed",
            ).model_dump(mode="json")
            return 201, self._save(db, live, state)

        return self._mutate(
            actor,
            key,
            "POST",
            f"/tasks/{task_id}/autonomy-sessions",
            canonical_digest(plan),
            command,
            replay_guard=lambda db, live, r: self._load(db, live, r["id"]),
        )

    def command(self, actor, ident, key, payload):
        body = self._body(SessionCommand, payload)

        def command(db, live):
            state = self._load(db, live, ident)
            self._revision(state, body, safety=body["command"] in {"cancel", "unknown"})
            state["state"] = self._effective(state)
            choice = body["command"]
            if choice in {"approve", "reject", "resume"}:
                self._human(live)
            if state["state"] in TERMINAL and choice != "cancel":
                raise Conflict("Terminal/unknown sessions cannot resume or replay.")
            if choice == "cancel":
                if state["cancel_requested"]:
                    return 200, state
                state["cancel_requested"] = True
                state["stop_reason"] = "cancel_requested"
                if state["state"] not in TERMINAL:
                    state["state"] = "unknown" if state["state"] == "running" else "cancelled"
            elif choice == "unknown":
                state["state"] = "unknown"
                state["stop_reason"] = "agent_unknown"
            elif choice == "revise":
                if state["actions"]:
                    raise Conflict(
                        "An attempted session cannot revise; reconcile and use fresh authority."
                    )
                self._basis(db, live, state["task_id"], body["plan"])
                state.update(
                    plan=body["plan"],
                    plan_digest=canonical_digest(body["plan"]),
                    state="proposed",
                    approved_by=None,
                    checkpoint=None,
                    permitted_action=None,
                )
                # Existing absolute deadline is never reset by revision/reapproval.
            elif choice == "approve":
                if state["state"] != "proposed" or body["plan_digest"] != state["plan_digest"]:
                    raise Conflict("Review requires the exact proposed plan.")
                policy, _ = self._basis(db, live, state["task_id"], state["plan"])
                self._current_policy(db, live, policy)
                state.update(approved_by=live["id"], state="ready")
                deadline = min(
                    _parse_utc(policy["expires_at"]),
                    datetime.now(UTC) + timedelta(seconds=state["plan"]["limits"]["max_seconds"]),
                )
                state["deadline"] = (
                    min(deadline, _parse_utc(state["deadline"])).isoformat()
                    if state["deadline"]
                    else deadline.isoformat()
                )
            elif choice == "reject":
                if state["state"] != "proposed":
                    raise Conflict("Only proposed sessions can be rejected.")
                state["state"] = "cancelled"
                state["stop_reason"] = "rejected"
            else:
                self._current(db, live, state)
                if state["state"] == "running":
                    raise Conflict(
                        "In-flight actions require receipt or unknown/cancel, not suspension."
                    )
                if choice == "resume":
                    if state["state"] not in {"checkpointed", "suspended"}:
                        raise Conflict("No resumable checkpoint.")
                    state["permitted_action"] = (state["checkpoint"] or {}).get("action_id")
                    state["state"] = "ready"
                elif choice == "checkpoint":
                    checkpoint = body["checkpoint"]
                    if checkpoint["action_id"] and checkpoint["action_id"] not in {
                        a["action_id"] for a in state["plan"]["acquisitions"]
                    }:
                        raise Conflict("Checkpoint cannot expand capabilities.")
                    state.update(state="checkpointed", checkpoint=checkpoint, permitted_action=None)
                elif choice == "suspend":
                    state.update(state="suspended", permitted_action=None)
                elif choice == "complete":
                    state["state"] = "completed"
                    state["stop_reason"] = "criteria_reported"
            state["revision"] += 1
            return 200, self._save(db, live, state)

        return self._mutate(
            actor,
            key,
            "POST",
            f"/autonomy-sessions/{ident}/commands",
            canonical_digest(body),
            command,
            replay_guard=lambda db, live, r: self._load(db, live, ident),
        )

    @staticmethod
    def _revision(state, body, *, safety=False):
        if state["revision"] != body["expected_revision"]:
            raise Conflict("Autonomy revision changed.")
        if state["revision"] >= 128 and not safety:
            raise Conflict("Autonomy transition budget exhausted; cancel/reconcile only.")

    def start(self, worker, ident, key, payload):
        body = self._body(StartAction, payload)

        def command(db, live):
            state = self._load(db, live, ident)
            self._revision(state, body)
            declaration = self._current(db, live, state)
            plan = state["plan"]
            if (
                state["state"] != "ready"
                or plan["profile"] == "observation-only"
                or "capability_prepare" not in plan["tools"]
            ):
                raise Conflict("Session is not ready for preparation tools.")
            if self._policy_reasons(plan, declaration):
                raise Forbidden(
                    "Capability escalation requires separate human authority; effects blocked."
                )
            if len(state["actions"]) >= plan["limits"]["max_actions"]:
                raise Conflict("Autonomy action budget exhausted.")
            op, policy, expiry = self._operation_current(db, live, body["operation_id"])
            binding = json.loads(op["binding_json"])
            if policy["id"] != plan["capability"]["policy_id"] or op["action_id"] not in {
                a["action_id"] for a in plan["acquisitions"]
            }:
                raise Forbidden("Action is outside the reviewed autonomy plan.")
            if not plan["public_auto"] and state["permitted_action"] != op["action_id"]:
                raise Conflict("Human checkpoint resume required for this action.")
            if operation_session(db, live["org_id"], op["id"]):
                raise Conflict("Operation already bound; no replay or replacement nonce.")
            issued = db.execute(
                "SELECT 1 FROM events WHERE org_id=? AND task_id=? "
                "AND event_type='capability.broker_package_issued' "
                "AND json_extract(payload_json,'$.operation_id')=? "
                "AND json_extract(payload_json,'$.package_digest')=? "
                "AND json_extract(payload_json,'$.mode')='synthetic'",
                (live["org_id"], state["task_id"], op["id"], body["package_digest"]),
            ).fetchone()
            used = db.execute(
                "SELECT 1 FROM events WHERE org_id=? AND event_type IN "
                "('capability.broker_claimed','capability.broker_journal_reconciled') "
                "AND json_extract(payload_json,'$.operation_id')=?",
                (live["org_id"], op["id"]),
            ).fetchone()
            if not issued or used:
                raise Conflict("Require an unclaimed issued synthetic package.")
            action = dict(
                autonomy_plan_digest=state["plan_digest"],
                task_plan_digest=plan["task_plan_digest"],
                workspace_id=state["workspace_id"],
                operation_id=op["id"],
                package_digest=body["package_digest"],
                nonce=uuid.uuid4().hex + uuid.uuid4().hex,
                worker_id=live["id"],
                binding=binding,
                decision="auto_approved" if plan["public_auto"] else "human_checkpoint",
                receipt=None,
            )
            state["actions"].append(action)
            state.update(
                state="running",
                permitted_action=None,
                lease_until=min(
                    expiry, _parse_utc(state["deadline"]), datetime.now(UTC) + timedelta(seconds=5)
                ).isoformat(),
            )
            self._event(
                db,
                live,
                state["project_id"],
                state["task_id"],
                "autonomy.action_bound",
                state["revision"] + 1,
                {"session_id": ident, **action},
            )
            state["revision"] += 1
            return 201, self._save(db, live, state)

        return self._mutate(
            worker,
            key,
            "POST",
            f"/worker/autonomy-sessions/{ident}/actions",
            canonical_digest(body),
            command,
            audience="worker",
            replay_guard=lambda db, live, r: self._load(db, live, ident),
        )

    def update_action(self, worker, ident, key, payload, *, receipt=False):
        body = self._body(ActionUpdate, payload)

        def command(db, live):
            state = self._load(db, live, ident)
            self._revision(state, body, safety=receipt)
            if not state["actions"]:
                raise Conflict("No action to reconcile.")
            action = state["actions"][-1]
            if (action["operation_id"], action["nonce"], action["worker_id"]) != (
                body["operation_id"],
                body["nonce"],
                live["id"],
            ) or action["receipt"] is not None:
                raise Conflict("Action identity changed or receipt is already final.")
            if receipt:
                self._operation(db, live, action["operation_id"])
                row = db.execute(
                    "SELECT payload_json FROM events WHERE org_id=? "
                    "AND event_type='capability.broker_journal_reconciled' "
                    "AND json_extract(payload_json,'$.operation_id')=? LIMIT 1",
                    (live["org_id"], action["operation_id"]),
                ).fetchone()
                if not row:
                    raise Conflict(
                        "Reconcile the original broker journal first; no asserted receipts."
                    )
                evidence = json.loads(row[0])
                bundle = evidence["journal"]
                last = bundle["receipts"][-1]
                if bundle["package_digest"] != action["package_digest"]:
                    raise Conflict("Receipt package differs.")
                action["receipt"] = AutonomyReceipt(
                    bundle_digest=evidence["placement"]["bundle_digest"],
                    artifact_sha256=bundle["content_sha256"],
                    request_digest=last["request_digest"],
                    response_digest=last["response_digest"],
                    inventory_digest=last["inventory_digest"],
                    installed_packages=last["installed_packages"],
                    bytes_retained=bundle["size"],
                    elapsed_ms=last["elapsed_ms"],
                    exit_code=last["exit_code"],
                    state=bundle["state"],
                    termination_confirmed=last["termination_confirmed"],
                ).model_dump(mode="json")
                effective = self._effective(state)
                if effective == "unknown" or bundle["state"] == "unknown":
                    state["state"] = "unknown"
                    state["stop_reason"] = state["stop_reason"] or "effect_unknown"
                elif evidence["slot_released"]:
                    state["state"] = "ready"
                else:
                    state["state"] = "failed"
                    state["stop_reason"] = "receipt_failed"
            else:
                require_session_authority(self, db, live, action["operation_id"])
                _, _, expiry = self._operation_current(db, live, action["operation_id"])
                state["lease_until"] = min(
                    expiry, _parse_utc(state["deadline"]), datetime.now(UTC) + timedelta(seconds=5)
                ).isoformat()
            state["revision"] += 1
            return 200, self._save(db, live, state)

        path = "receipt" if receipt else "heartbeat"
        return self._mutate(
            worker,
            key,
            "POST",
            f"/worker/autonomy-sessions/{ident}/{path}",
            canonical_digest(body),
            command,
            audience="worker",
            replay_guard=lambda db, live, r: self._load(db, live, ident),
        )

    def _projection(self, db, actor, state):
        result = dict(state)
        result["state"] = self._effective(state)
        reasons = []
        policy = self._policy(db, actor, state["plan"]["capability"]["policy_id"])
        declaration = json.loads(policy["declaration_json"])
        reasons.extend(self._policy_reasons(state["plan"], declaration))
        try:
            self._current(db, actor, state)
            if state["state"] == "running":
                action = state["actions"][-1]
                worker = self._live_principal(
                    db, {"id": action["worker_id"], "org_id": actor["org_id"]}, audience="worker"
                )
                self._operation_current(db, worker, action["operation_id"])
        except (Conflict, Forbidden, NotFound):
            reasons.append("authority_not_current")
            if state["state"] == "running":
                result["state"] = "unknown"
        if self._effective(state) != state["state"]:
            reasons.append("lease_or_deadline_expired")
        if result["state"] in {"checkpointed", "suspended", "unknown", "expired"}:
            reasons.append(result["state"])
        if state["plan"]["profile"] == "observation-only":
            reasons.append("observation_only")
        result.update(
            execution_enabled=False,
            provider_enabled=False,
            live_activation="blocked",
            billing=None,
            declaration=declaration,
            pending_approval=result["state"] in {"proposed", "checkpointed", "suspended"},
            blocked_reasons=sorted(set(reasons)),
        )
        return result

    def get(self, actor, ident):
        with _connect(self.database_path) as db:
            db.execute("BEGIN")
            live = self._live_principal(db, actor)
            return self._projection(db, live, self._load(db, live, ident))

    def list(self, actor, task_id):
        with _connect(self.database_path) as db:
            db.execute("BEGIN")
            live = self._live_principal(db, actor)
            self._task(db, live, task_id)
            rows = db.execute(
                "SELECT payload_json FROM events WHERE org_id=? AND task_id=? "
                "AND event_type='autonomy.state' AND resource_revision=1 ORDER BY cursor LIMIT 33",
                (live["org_id"], task_id),
            ).fetchall()
            if len(rows) > 32:
                raise Conflict("Autonomy projection exceeds bound.")
            return {
                "sessions": [
                    self._projection(db, live, self._load(db, live, json.loads(r[0])["id"]))
                    for r in rows
                ]
            }
