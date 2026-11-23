"""Salad route hooks for the generic remote-route core (see ``remote_execution_hooks``)."""

import json
from datetime import UTC, datetime

from pydantic import ValidationError

from .execution_targets import require_admission_hook
from .remote_execution_hooks import RouteHooks
from .remote_execution_models import derived_salad_argv
from .salad_workload_contract import capability_network_matches
from .store import Conflict, Forbidden, _parse_utc, canonical_digest


class SaladRouteHooks(RouteHooks):
    group_cleanup = True

    def refuse(self, proposal):
        if proposal["selection"]["target"] == "salad_inference":
            raise Conflict("Salad inference is reserved, not executable.")

    def on_propose(self, db, op, selection, admit_workload):
        if selection["salad"]:
            admit_workload(db, op["project_id"], selection, op["worker_principal_id"])
            salad = selection["salad"]
            if db.execute(
                "SELECT 1 FROM events WHERE event_type='remote_execution.state' "
                "AND json_extract(payload_json,'$.proposal.selection.salad.organization')=? "
                "AND json_extract(payload_json,'$.proposal.selection.salad.project')=? "
                "AND json_extract(payload_json,'$.proposal.selection.salad.group')=?",
                (salad["organization"], salad["project"], salad["group"]),
            ).fetchone():
                raise Conflict("Salad group already reserved; one immutable group per attempt.")

    def cleanup_group(self, selection):
        return (selection.get("salad") or {}).get("group")

    def open_readonly(self, database_path):
        from .salad_group_cleanup import open_readonly

        return open_readonly(database_path)

    def read_route_snapshots(self, database_path, *, org_id, admit):
        from .salad_group_cleanup import read_route_snapshots

        return read_route_snapshots(database_path, org_id=org_id, admit=admit)

    def parse_cleanup_audit(self, audit):
        from .salad_group_cleanup import CleanupAudit

        try:
            return CleanupAudit.model_validate(audit).model_dump(mode="json", exclude_unset=True)
        except ValidationError as error:
            raise ValueError("unsupported cleanup audit shape") from error

    def claim_path(self, ident):
        return f"/internal/salad/{ident}/claim"

    def claim_proxy(self, store, db, live, ident, effect_id, policy, preflight, recheck):
        """Use-time admission and one-use nonce, in the caller's route transaction."""
        state = store._load(db, live, ident)
        proposal = state["proposal"]
        op, package = store._operation(db, live, proposal)
        if (
            live["id"] != op["worker_principal_id"]
            or (live["org_id"], state["project_id"]) != (policy.org_id, policy.project_id)
            or proposal["selection"] != policy.target.selection.model_dump(mode="json")
            or not state["approved_by"]
            or not state["effects"]
            or state["effects"][-1]["id"] != effect_id
            or any(r["effect_id"] == effect_id for r in state["receipts"])
        ):
            raise Forbidden("Salad proxy authority binding mismatch.")
        effect = state["effects"][-1]
        action = effect["action"]
        if action in {"create", "start"}:
            store._current(db, live, state)
            # Salad-only path: a missing hook fails closed, never skips the budget.
            try:
                hook = require_admission_hook(proposal["selection"]["target"])
            except ValueError:
                raise Conflict("Salad target has no admission hook; refusing.") from None
            state["budget_binding"] = hook(db, live, proposal, op)
            if state["desired"] != "run" or state["reallocated"]:
                raise Conflict("Salad proxy execution is fenced.")
        elif action in {"stop", "delete"} and state["desired"] != "stop":
            raise Conflict("Salad stop-only authority required.")
        if action in {"start", "stop", "delete"} and not state["provider_id"]:
            raise Conflict("Exact Salad provider identity required; reconcile by name first.")
        if action == "observe" and not any(e["action"] == "create" for e in state["effects"]):
            raise Conflict("Salad observation requires an existing create intent.")
        profile = proposal["selection"]["salad"]
        if profile["group"] != "attempt-" + proposal["operation_id"]:
            raise Conflict("Salad group must bind the unique operation.")
        execution = profile.get("execution")
        spec = package["plan"]["execution_spec"]
        if (
            execution is None
            or execution["max_output_bytes"] > spec["max_output_bytes"]
            or execution["max_cost_cents"] > package["plan"]["budget_cents"]
        ):
            raise Conflict("Salad output limits exceed admitted package.")
        try:
            derived_salad_argv(
                execution,
                spec["argv"],
                proposal["operation_id"],
                proposal["epoch"],
                proposal["selection"]["resources"]["max_seconds"],
            )
        except ValueError:
            raise Conflict("Salad argv differs from admitted package.") from None
        if action in {"create", "start"}:
            capability = db.execute(
                "SELECT * FROM capability_policies WHERE org_id=? AND project_id=? "
                "AND task_id=? AND declaration_digest=? AND status='approved'",
                (
                    live["org_id"],
                    state["project_id"],
                    state["task_id"],
                    execution["capability_digest"],
                ),
            ).fetchone()
            if capability is None:
                raise Conflict("Approved Salad capability policy required.")
            store._current_policy(db, live, capability)
            declaration = json.loads(capability["declaration_json"])
            if (
                declaration["image_digest"]
                != proposal["selection"]["manifests"]["image"].split("@")[1]
                or declaration["actions"]
                or declaration["credential_refs"]
                or not capability_network_matches(execution, declaration)
            ):
                raise Conflict(
                    "Salad runtime acquisition/credential/network capabilities unsupported."
                )
        # The current base package is deliberately a rejecting Kubernetes contract.
        # This overlay cannot reinterpret its network-none claim as Salad isolation.
        age = (datetime.now(UTC) - _parse_utc(preflight["observed_at"])).total_seconds()
        if (
            not 0 <= age <= 5
            or preflight["selection_digest"] != canonical_digest(proposal["selection"])
            or preflight["route_digest"] != state["proposal_digest"]
            or (
                action in {"create", "start"}
                and (preflight["available"] is not True or preflight["quota_ok"] is not True)
            )
            or (action == "create" and preflight["group_absent"] is not True)
            or (
                action == "start"
                and (
                    preflight["state"] != "stopped"
                    or preflight["provider_id"] != state["provider_id"]
                )
            )
        ):
            raise Conflict("Fresh exact Salad target preflight required.")
        claimed = db.execute(
            "SELECT 1 FROM events WHERE event_type='salad_proxy.claim' "
            "AND json_extract(payload_json,'$.nonce')=?",
            (effect_id,),
        ).fetchone()
        if recheck:
            if not claimed:
                raise Conflict("Salad proxy has no committed claim.")
            return 200, state
        if claimed:
            raise Conflict("Salad proxy nonce consumed; reconcile without retry.")
        claim = dict(
            nonce=effect_id,
            route_id=ident,
            route_digest=state["proposal_digest"],
            operation_id=proposal["operation_id"],
            attempt_id=proposal["attempt_id"],
            lease_id=proposal["lease_id"],
            epoch=proposal["epoch"],
            package_digest=proposal["package_digest"],
            action=action,
            preflight_digest=canonical_digest(preflight),
        )
        store._event(db, live, state["project_id"], state["task_id"], "salad_proxy.claim", 1, claim)
        return 201, state

    def project_state(self, state):
        if state["proposal"]["selection"]["target"] == "salad_container":
            from .salad_proxy_contract import safety_projection

            state["proxy_safety"] = safety_projection()


def salad_route_hooks():
    return SaladRouteHooks()
