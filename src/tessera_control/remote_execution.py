"""Transactional remote-route overlay on admitted operations; offline activation only.

Events retain proposals, one-shot effect intents and receipts in the existing
control backup/held-recovery boundary. No provider client or credential lives here.
"""

import json
import uuid
from datetime import UTC, datetime

from .capabilities import CapabilityStore
from .experiments import admitted_attempt_package_identity
from .programs import program_context
from .remote_execution_models import (
    ExecutionSelection,
    ProviderReadback,
    RemoteCommand,
    RemoteExecutionPackage,
    RemoteProposal,
    TargetPolicy,
)
from .store import Conflict, Forbidden, NotFound, _connect, _parse_utc, canonical_digest, utc_now


class RemoteExecutionStore(CapabilityStore):
    def __init__(self, state_dir, *, policies=()):
        super().__init__(state_dir)
        # Snapshot operator configuration. There is no policy/credential write API.
        self.policies = tuple(
            TargetPolicy.model_validate_json(p.model_dump_json()) for p in policies
        )

    def _load(self, db, actor, ident):
        row = db.execute(
            "SELECT payload_json FROM events WHERE org_id=? "
            "AND event_type='remote_execution.state' "
            "AND json_extract(payload_json,'$.id')=? ORDER BY cursor DESC LIMIT 1",
            (actor["org_id"], ident),
        ).fetchone()
        if row is None:
            raise NotFound("Remote execution route not found.")
        state = json.loads(row[0])
        self._project(db, actor, state["project_id"])
        return state

    def _save(self, db, actor, state):
        state["revision"] += 1
        self._event(
            db,
            actor,
            state["project_id"],
            state["task_id"],
            "remote_execution.state",
            state["revision"],
            state,
        )
        return state

    def _operation(self, db, actor, proposal):
        op = db.execute(
            "SELECT * FROM external_operations WHERE operation_id=? AND org_id=?",
            (proposal["operation_id"], actor["org_id"]),
        ).fetchone()
        if op is None:
            raise NotFound("Admitted operation not found.")
        self._project(db, actor, op["project_id"])
        row = db.execute(
            "SELECT * FROM execution_runtime_packages WHERE operation_id=?", (op["operation_id"],)
        ).fetchone()
        if row is None:
            raise Conflict("A retained remote runtime package is required.")
        package = json.loads(row["package_json"])
        if (
            op["adapter_version"] != "kubernetes-job-v1"
            or canonical_digest(package) != proposal["package_digest"]
            or row["package_digest"] != proposal["package_digest"]
            or op["package_digest"] != proposal["package_digest"]
            or op["experiment_attempt_id"] != proposal["attempt_id"]
            or op["lease_id"] != proposal["lease_id"]
            or op["epoch"] != proposal["epoch"]
            or package["plan_digest"] != proposal["plan_digest"]
            or package["pinned_image"] != proposal["selection"]["manifests"]["image"]
            or package["snapshot_digest"] != proposal["selection"]["manifests"]["data"]
            or proposal["selection"]["resources"]["max_seconds"]
            > package["plan"]["max_duration_seconds"]
        ):
            raise Conflict("Remote tenant/plan/attempt/package/manifest binding mismatch.")
        return dict(op), package

    def _reviews(self, db, actor, proposal, project_id):
        context = program_context(db, actor, proposal["study_id"])
        if context["project_id"] != project_id:
            raise Conflict("Program review project mismatch.")
        if not any(
            a["id"] == proposal["attempt_id"]
            for execution in context["basis"]["executions"]
            for a in execution["admissions"]
        ):
            raise Conflict("Program study does not link this admitted experiment attempt.")
        reviews = {r["id"]: r for r in context["reviews"]}
        selected = []
        for field, action in (("program_review_id", "plan"), ("source_review_id", "source_use")):
            review = reviews.get(proposal[field])
            if (
                not review
                or not review["is_effective_review"]
                or not review["is_proposal_head"]
                or review["scope"]["action"] != action
            ):
                raise Conflict("Current human program and source-use reviews are required.")
            reviewer = self._live_principal(
                db, {"id": review["reviewer_principal_id"], "org_id": actor["org_id"]}
            )
            self._human(reviewer)
            self._project(db, reviewer, project_id)
            selected.append(review["scope"])
        plan, source = selected
        if (
            plan["outcome"] != source["outcome"]
            or plan["brief"]["missing_prerequisites"]
            or not set(plan["brief"]["data_source_ids"]) <= set(source["source_ids"])
        ):
            raise Conflict("Program/source-use review is incomplete.")
        target = proposal["selection"]["target"]
        if plan["brief"]["compute"]["target"] != target:
            raise Conflict("Program did not review this exact remote target.")
        protocol = plan.get("evaluation_protocol")
        if protocol:
            manifests = proposal["selection"]["manifests"]
            if protocol["identity"] != {
                f"{name}_digest": manifests[name] for name in ("code", "data", "environment")
            }:
                raise Conflict("Program evaluation manifests mismatch.")

    def _policy(self, proposal):
        if proposal["selection"]["target"] == "salad_inference":
            raise Conflict("Salad inference is reserved, not executable.")
        if not any(
            p.selection == ExecutionSelection.model_validate(proposal["selection"])
            for p in self.policies
        ):
            raise Forbidden("Exact execution target/resources/manifests are not allowlisted.")

    def _current(self, db, actor, state):
        if (
            db.execute("SELECT state FROM control_recovery WHERE singleton=1").fetchone()[0]
            != "active"
        ):
            raise Forbidden("Held recovery has no remote execution authority.")
        proposal = state["proposal"]
        op, package = self._operation(db, actor, proposal)
        worker = self._live_principal(
            db, {"id": op["worker_principal_id"], "org_id": op["org_id"]}, audience="worker"
        )
        if actor["kind"] == "worker" and actor["id"] != worker["id"]:
            raise Forbidden("Wrong dispatcher worker.")
        grant = self._active_worker_grant(db, worker, op["grant_id"])
        lease = db.execute(
            "SELECT * FROM execution_leases WHERE id=?", (op["lease_id"],)
        ).fetchone()
        if (
            grant["epoch"] != proposal["epoch"]
            or lease["epoch"] != proposal["epoch"]
            or _parse_utc(lease["expires_at"]) <= datetime.now(UTC)
            or _parse_utc(lease["hard_expires_at"]) <= datetime.now(UTC)
            or op["desired_state"] != "admitted"
        ):
            raise Conflict("Remote execution lease/epoch is fenced.")
        admitted_attempt_package_identity(db, worker, package, proposal["attempt_id"])
        self._reviews(db, actor, proposal, op["project_id"])
        self._policy(proposal)
        if state["approved_by"]:
            reviewer = self._live_principal(
                db, {"id": state["approved_by"], "org_id": op["org_id"]}
            )
            self._human(reviewer)
            self._project(db, reviewer, op["project_id"])
        return op

    def _queue_kubernetes(self, db, actor, state, op):
        """Atomically bind a reviewed route to the existing worker dispatch outbox.

        A route is the human decision; the old standalone dispatch endpoint must
        not be used to create a second decision for the same operation.
        """
        proposal = state["proposal"]
        package = json.loads(
            db.execute(
                "SELECT package_json FROM execution_runtime_packages WHERE operation_id=?",
                (op["operation_id"],),
            ).fetchone()[0]
        )
        attempt = db.execute(
            "SELECT * FROM research_experiment_admitted_attempts WHERE id=?",
            (proposal["attempt_id"],),
        ).fetchone()
        if (
            state["effects"]
            or package.get("provider_enabled") is not False
            or op["profile_version"] != "development-minikube-opencode-v1"
            or attempt is None
            or attempt["operation_id"] != op["operation_id"]
            or attempt["package_digest"] != proposal["package_digest"]
            or attempt["outcome"] is not None
            or db.execute(
                "SELECT 1 FROM kubernetes_dispatches WHERE operation_id=? OR admitted_attempt_id=?",
                (op["operation_id"], proposal["attempt_id"]),
            ).fetchone()
        ):
            raise Conflict("Route cannot queue this admitted Kubernetes package twice.")
        now = utc_now()
        dispatch = dict(
            id="kubernetes_dispatch_" + uuid.uuid4().hex,
            operation_id=op["operation_id"],
            admitted_attempt_id=proposal["attempt_id"],
            org_id=actor["org_id"],
            project_id=state["project_id"],
            grant_id=op["grant_id"],
            lease_id=op["lease_id"],
            worker_principal_id=op["worker_principal_id"],
            epoch=op["epoch"],
            package_digest=proposal["package_digest"],
            target_digest=proposal["selection"]["target_digest"],
            profile_version=op["profile_version"],
            state="queued",
            claimed_at=None,
            claimed_by_principal_id=None,
            created_at=now,
            updated_at=now,
            created_by_principal_id=actor["id"],
        )
        db.execute(
            """INSERT INTO kubernetes_dispatches
            (id,operation_id,admitted_attempt_id,org_id,project_id,grant_id,lease_id,
             worker_principal_id,epoch,package_digest,target_digest,profile_version,state,
             claimed_at,claimed_by_principal_id,created_at,updated_at,created_by_principal_id)
            VALUES (:id,:operation_id,:admitted_attempt_id,:org_id,:project_id,:grant_id,:lease_id,
            :worker_principal_id,:epoch,:package_digest,:target_digest,:profile_version,:state,
            :claimed_at,:claimed_by_principal_id,:created_at,:updated_at,:created_by_principal_id)""",
            dispatch,
        )
        state["dispatch_id"] = dispatch["id"]
        self._event(
            db,
            actor,
            state["project_id"],
            state["task_id"],
            "experiment.kubernetes_dispatch_approved",
            state["revision"],
            {
                "dispatch_id": dispatch["id"],
                "operation_id": op["operation_id"],
                "admitted_attempt_id": proposal["attempt_id"],
                "target_digest": dispatch["target_digest"],
                "profile_version": dispatch["profile_version"],
                "execution_dispatched": True,
            },
        )

    def propose(self, actor, task_id, key, payload):
        body = self._body(RemoteProposal, payload)

        def command(db, live):
            task = self._task(db, live, task_id)
            op, _ = self._operation(db, live, body)
            if op["task_id"] != task_id or task["current_plan_digest"] != body["plan_digest"]:
                raise Conflict("Task plan mismatch.")
            count = db.execute(
                "SELECT count(*) FROM events WHERE org_id=? AND task_id=? "
                "AND event_type='remote_execution.state' AND resource_revision=1",
                (live["org_id"], task_id),
            ).fetchone()[0]
            if count >= 32:
                raise Conflict("Task remote proposal limit reached.")
            if body["selection"]["salad"]:
                salad = body["selection"]["salad"]
                if db.execute(
                    "SELECT 1 FROM events WHERE event_type='remote_execution.state' "
                    "AND json_extract(payload_json,'$.proposal.selection.salad.organization')=? "
                    "AND json_extract(payload_json,'$.proposal.selection.salad.project')=? "
                    "AND json_extract(payload_json,'$.proposal.selection.salad.group')=?",
                    (salad["organization"], salad["project"], salad["group"]),
                ).fetchone():
                    raise Conflict("Salad group already reserved; one immutable group per attempt.")
            if (
                db.execute(
                    "SELECT 1 FROM kubernetes_dispatches WHERE operation_id=?",
                    (body["operation_id"],),
                ).fetchone()
                or db.execute(
                    "SELECT 1 FROM events WHERE org_id=? AND event_type='remote_execution.state' "
                    "AND json_extract(payload_json,'$.proposal.operation_id')=?",
                    (live["org_id"], body["operation_id"]),
                ).fetchone()
            ):
                raise Conflict("Operation already has a route; never retarget or retry it.")
            routing_package = RemoteExecutionPackage(
                org_id=live["org_id"],
                project_id=op["project_id"],
                task_id=task_id,
                proposal=body,
            ).model_dump(mode="json")
            state = dict(
                id="route_" + uuid.uuid4().hex,
                org_id=live["org_id"],
                project_id=op["project_id"],
                task_id=task_id,
                proposal=body,
                proposal_digest=canonical_digest(routing_package),
                routing_package=routing_package,
                revision=0,
                approved_by=None,
                proposed_by=live["id"],
                state="proposed",
                desired="hold",
                effects=[],
                receipts=[],
                dispatch_id=None,
                provider_id=None,
                allocation_id=None,
                reallocated=False,
                provider_status="not_queried",
                termination_confirmed=False,
                cancellation="not_requested",
                provider_enabled=False,
                live_activation="blocked",
                evidence="synthetic",
                cost_cents=None,
            )
            return 201, self._save(db, live, state)

        return self._mutate(
            actor,
            key,
            "POST",
            f"/tasks/{task_id}/remote-executions",
            canonical_digest(body),
            command,
            replay_guard=lambda db, live, r: self._load(db, live, r["id"]),
        )

    def command(self, actor, ident, key, payload):
        body = self._body(RemoteCommand, payload)

        def command(db, live):
            self._human(live)
            state = self._load(db, live, ident)
            if (
                state["revision"] != body["expected_revision"]
                or state["proposal_digest"] != body["proposal_digest"]
            ):
                raise Conflict("Review the current exact remote proposal.")
            choice = body["command"]
            if choice in {"pause", "cancel"}:
                state.update(desired="stop", cancellation="pending", state="stop_requested")
                # Pause means stop/hold, never process suspension or automatic resume.
                op, _ = self._operation(db, live, state["proposal"])
                grant = self._grant(db, live, op["grant_id"])
                if grant["status"] == "active":
                    updated, operation_ids = self._request_grant_cancellation(
                        db, grant, grant["revision"]
                    )
                    self._event(
                        db,
                        live,
                        grant["project_id"],
                        grant["task_id"],
                        "execution.grant_cancel_requested",
                        updated["revision"],
                        {
                            "grant_id": grant["id"],
                            "epoch": updated["epoch"],
                            "operation_ids": operation_ids,
                            "cancellation": self._cancellation_payload(db, updated),
                            "operational_only": True,
                        },
                    )
            else:
                op = self._current(db, live, state)
                if choice == "approve" and state["state"] == "proposed":
                    state.update(approved_by=live["id"], state="reviewed")
                elif choice == "start" and state["state"] == "reviewed":
                    if state["proposal"]["selection"]["target"] == "kubernetes":
                        self._queue_kubernetes(db, live, state, op)
                    state.update(desired="run", state="queued")
                else:
                    raise Conflict("No resumable route; reconcile and obtain fresh authority.")
            return 200, self._save(db, live, state)

        return self._mutate(
            actor,
            key,
            "POST",
            f"/remote-executions/{ident}/commands",
            canonical_digest(body),
            command,
            replay_guard=lambda db, live, r: self._load(db, live, ident),
        )

    def begin(self, worker, ident, action, *, preflight_digest=None):
        """No idempotent cached effect permission: commit one intent, then call provider."""
        if action not in {"create", "start", "stop", "delete", "observe"}:
            raise Conflict("Unsupported remote effect.")

        def command(db, live):
            state = self._load(db, live, ident)
            op, _ = self._operation(db, live, state["proposal"])
            if live["id"] != op["worker_principal_id"]:
                raise Forbidden("Wrong dispatcher worker.")
            if state.get("dispatch_id") is not None:
                raise Conflict(
                    "Kubernetes route uses the claimed dispatch outbox, not route effects."
                )
            if len(state["effects"]) >= 128:
                raise Conflict(
                    "Remote observation budget exhausted; manual reconciliation required."
                )
            if action != "observe" and any(e["action"] == action for e in state["effects"]):
                raise Conflict("Effect already attempted; read-only reconciliation required.")
            if action in {"create", "start"}:
                self._current(db, live, state)
                if action == "start" and state["proposal"]["selection"]["target"] == "kubernetes":
                    raise Conflict(
                        "Kubernetes create includes start; separate restart is forbidden."
                    )
                if (
                    not state["approved_by"]
                    or state["desired"] != "run"
                    or state["reallocated"]
                    or state["state"] not in {"queued", "stopped"}
                    or not preflight_digest
                ):
                    raise Conflict("Fresh preflight and reviewed remote authority required.")
                if action == "start" and (
                    not state["termination_confirmed"] or state["provider_status"] != "stopped"
                ):
                    raise Conflict("Exact stopped-group readback required before start.")
                if action == "create" and state["effects"]:
                    raise Conflict("Observed/uncertain operations cannot be created again.")
            elif action == "stop":
                # Expired/revoked leases retain narrowly scoped stop-only authority.
                if state["desired"] != "stop" and op["desired_state"] != "cancel_requested":
                    raise Conflict("Human stop/pause or grant cancellation required.")
            elif action == "delete" and (
                not state["termination_confirmed"] or state["desired"] != "stop"
            ):
                raise Conflict("Confirmed termination and stop intent required before deletion.")
            effect = dict(
                id=uuid.uuid4().hex,
                action=action,
                requested_at=utc_now(),
                epoch=state["proposal"]["epoch"],
                preflight_digest=preflight_digest,
            )
            state["effects"].append(effect)
            state["state"] = "unknown"  # A crash/lost reply is durably unresolved.
            if action == "stop":
                state["cancellation"] = "unknown"
                state["desired"] = "stop"
            return 201, self._save(db, live, state)

        return self._mutate(
            worker,
            uuid.uuid4().hex,
            "POST",
            f"/internal/remote/{ident}/{action}",
            canonical_digest([action, preflight_digest]),
            command,
            audience="worker",
        )[1]

    def record(self, worker, ident, effect_id, readback):
        body = self._body(ProviderReadback, readback)

        def command(db, live):
            state = self._load(db, live, ident)
            op, _ = self._operation(db, live, state["proposal"])
            if op["worker_principal_id"] != live["id"]:
                raise Forbidden("Wrong receipt worker.")
            if state["effects"][-1]["id"] != effect_id:
                raise Conflict("Late/out-of-order receipt cannot overwrite current evidence.")
            effect = state["effects"][-1]
            if body["provenance"] != "synthetic":
                raise Forbidden("Live provider evidence intake is not activated.")
            observed = _parse_utc(body["observed_at"])
            if not _parse_utc(effect["requested_at"]) <= observed <= datetime.now(UTC):
                raise Conflict("Receipt timestamp outside the effect window.")
            if state["provider_id"] and body["provider_id"] not in {None, state["provider_id"]}:
                raise Conflict("Provider identity substitution.")
            for link in body["evidence"]:
                if link["manifest_id"] or link["custody"] == "quarantined":
                    manifest = db.execute(
                        "SELECT * FROM artifact_manifests WHERE id=? AND project_id=? "
                        "AND operation_id=? AND epoch=? AND plan_digest=?",
                        (
                            link["manifest_id"],
                            state["project_id"],
                            op["operation_id"],
                            op["epoch"],
                            state["proposal"]["plan_digest"],
                        ),
                    ).fetchone()
                    if manifest is None or manifest["manifest_digest"] != link["sha256"]:
                        raise Conflict("Evidence has no matching operation-bound artifact custody.")
                    link["custody"] = "quarantined"
            if (
                state["allocation_id"]
                and body["allocation_id"]
                and state["allocation_id"] != body["allocation_id"]
            ):
                state["reallocated"] = True
            state["provider_id"] = state["provider_id"] or body["provider_id"]
            state["allocation_id"] = state["allocation_id"] or body["allocation_id"]
            state["provider_status"] = body["state"]
            state["termination_confirmed"] = (
                body["termination_confirmed"] and not state["reallocated"]
            )
            state["cost_cents"] = body["cost_cents"]
            state["state"] = "unknown" if state["reallocated"] else body["state"]
            if state["cancellation"] != "not_requested":
                state["cancellation"] = (
                    "confirmed"
                    if state["termination_confirmed"]
                    else ("pending" if body["response_status"] == 202 else "unknown")
                )
            state["receipts"].append(
                dict(
                    effect_id=effect_id,
                    target=state["proposal"]["selection"]["target"],
                    route_digest=state["proposal_digest"],
                    **body,
                )
            )
            return 200, self._save(db, live, state)

        return self._mutate(
            worker,
            effect_id,
            "POST",
            f"/internal/remote/{ident}/receipts",
            canonical_digest(body),
            command,
            audience="worker",
        )[1]

    def get(self, actor, ident):
        with _connect(self.database_path) as db:
            live = self._live_principal(db, actor, audience=actor.get("audience", "control"))
            state = self._load(db, live, ident)
            # Connection presence never renews a lease, cancels or grants execution.
            state["control_surface"] = "desktop_control_only"
            state["go_no_go"] = "no_go_live_activation_blocked"
            if state["proposal"]["selection"]["target"] == "salad_container":
                from .salad_proxy_contract import safety_projection

                state["proxy_safety"] = safety_projection()
            state["authority_status"] = "unreviewed"
            if state["approved_by"]:
                try:
                    self._current(db, live, state)
                except (Conflict, Forbidden, NotFound):
                    state["authority_status"] = "fenced_reconciliation_only"
                else:
                    state["authority_status"] = "current_offline_only"
            return state

    def claim_salad_proxy(self, worker, ident, effect_id, policy, preflight, *, recheck=False):
        """Server-only use-time admission and durable one-use nonce. No public API.

        A claim is not a new grant and cannot be replayed even after proxy restart.
        Policy is trusted composition, never supplied by an HTTP request.
        """

        def command(db, live):
            state = self._load(db, live, ident)
            proposal = state["proposal"]
            op, package = self._operation(db, live, proposal)
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
                self._current(db, live, state)
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
                or [execution["command"], *execution["argv"]] != spec["argv"]
                or execution["max_cost_cents"] > package["plan"]["budget_cents"]
            ):
                raise Conflict("Salad output limits exceed admitted package.")
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
                self._current_policy(db, live, capability)
                declaration = json.loads(capability["declaration_json"])
                if (
                    declaration["image_digest"]
                    != proposal["selection"]["manifests"]["image"].split("@")[1]
                    or declaration["actions"]
                    or declaration["credential_refs"]
                    or declaration["network"]
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
            self._event(
                db, live, state["project_id"], state["task_id"], "salad_proxy.claim", 1, claim
            )
            return 201, state

        return self._mutate(
            worker,
            uuid.uuid4().hex,
            "POST",
            f"/internal/salad/{ident}/claim",
            canonical_digest([ident, effect_id]),
            command,
            audience="worker",
        )[1]

    def list(self, actor, task_id):
        with _connect(self.database_path) as db:
            live = self._live_principal(db, actor)
            self._task(db, live, task_id)
            rows = db.execute(
                "SELECT payload_json FROM events WHERE org_id=? AND task_id=? "
                "AND event_type='remote_execution.state' AND resource_revision=1 LIMIT 33",
                (live["org_id"], task_id),
            ).fetchall()
            if len(rows) > 32:
                raise Conflict("Remote route listing exceeds bound.")
            return {"routes": [self.get(live, json.loads(r[0])["id"]) for r in rows]}
