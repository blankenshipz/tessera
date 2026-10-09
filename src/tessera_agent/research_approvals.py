"""Authenticated MCP state polling plus separately authenticated human record ports.

No approval is read from model text or config. record_source must be the installed
backend adapter, authorized to read current decisions/disclosures/command refs;
there is no default or human credential passed into the agent tool client.
"""

import hashlib

from .research_consumer_store import ConsumerBlocked


class MCPJourneyApprovals:
    def __init__(self, *, planner, journey_store, journey_id, record_source, experiment_reader):
        self.planner, self.store, self.id = planner, journey_store, journey_id
        self.record_source, self.experiment_reader = record_source, experiment_reader

    def _ref(self, name):
        return self.store.reference(self.id, name)

    def poll(self, journey_id):
        if journey_id != self.id:
            raise ConsumerBlocked("approval_journey_changed")
        refs = self.record_source.read_current_references(journey_id)
        snapshot = {"journey_id": journey_id, **refs}
        proposal_id = self._ref("proposal_id")
        if proposal_id:
            proposal = self.planner.mcp.call("tessera_read_proposal", {"proposal_id": proposal_id})
            if proposal.get("id") != proposal_id:
                raise ConsumerBlocked("approval_proposal_changed")
            review = self.record_source.read_human_review(proposal_id)
            if review is not None:
                if (
                    review.get("issuer_role") != "human"
                    or review.get("subject_id") != proposal_id
                    or review.get("project_id") != self.planner.binding["project_id"]
                    or review.get("org_id") != self.planner.binding["org_id"]
                    or review.get("decision") not in {"approved", "rejected"}
                ):
                    raise ConsumerBlocked("human_review_record_invalid")
                snapshot.update(
                    review_id=review["id"],
                    review_subject_id=proposal_id,
                    review_state=review["decision"],
                )
            promotion = proposal.get("promotion")
            if isinstance(promotion, dict):
                snapshot["approved_task_id"] = promotion["task_id"]
        # A current grant read is required even when the command body/key was
        # persisted before a crash. No admission renewal or owner impersonation.
        for command in snapshot.get("commands", {}).values():
            self._grant(command)
        return snapshot

    def _grant(self, command):
        grant = self.planner.mcp.call("tessera_authority_status", {"grant_id": command["grant_id"]})
        payload = command["payload"]
        bounds = grant.get("bounds", {})
        if (
            grant.get("id") != command["grant_id"]
            or grant.get("status") != "active"
            or grant.get("effective_status") != "active"
            or grant.get("revision") != payload["expected_revision"]
            or grant.get("epoch") != payload["expected_epoch"]
            or bounds.get("subject_credential_id") != self.planner.binding["credential_id"]
            or bounds.get("model") != payload["model"]
            or grant.get("project_id") != self.planner.binding["project_id"]
            or grant.get("org_id") != self.planner.binding["org_id"]
            or "sha256:" + hashlib.sha256(str(bounds.get("destination", "")).encode()).hexdigest()
            != self.planner.binding["destination_digest"]
            or payload["command"]["action"] not in bounds.get("actions", [])
        ):
            raise ConsumerBlocked("current_execution_authority_denied")
        return grant

    def consume_cancel(self, journey_id, command, attempt_id, key):
        if journey_id != self.id or attempt_id != self._ref("attempt_id"):
            raise ConsumerBlocked("cancel_attempt_changed")
        self._grant(command)
        from .research_control_receipts import consumption_reference

        response = self.planner.mcp.call(
            "tessera_consume_grant", {**command, "idempotency_key": key}
        )
        reference = consumption_reference(response, command["grant_id"], "execution:cancel")
        return {"id": reference["consumption_id"]}

    def read_cancel_status(self, journey_id, attempt_id):
        if journey_id != self.id or attempt_id != self._ref("attempt_id"):
            raise ConsumerBlocked("cancel_attempt_changed")
        command = self.record_source.read_status_command(attempt_id)
        if (
            command["payload"]["command"]["action"] != "attempt:status"
            or command["payload"]["command"]["attempt_id"] != attempt_id
        ):
            raise ConsumerBlocked("cancel_status_command_changed")
        self._grant(command)
        sequence = self.store.next_poll(self.id)
        if sequence > 64:
            raise ConsumerBlocked("attempt_poll_bound_reached")
        response = self.planner.mcp.call(
            "tessera_consume_grant",
            {
                **command,
                "idempotency_key": f"research-cancel-status-{journey_id}-{sequence}",
            },
        )
        return {
            "attempt_id": attempt_id,
            "status": self.experiment_reader.status(response, attempt_id),
        }
