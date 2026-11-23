"""Explicit allowlisted HTTP calls; no generic proxy and no response/error logging."""

import base64
import hashlib
import json
import ssl
from urllib.parse import quote, urlencode

import httpx

from tessera_mcp.config import Config


class ControlError(Exception):
    pass


class ControlRefusal(ControlError):
    """A 409 submission refusal carrying the control's stable ``reason`` code."""

    def __init__(self, reason: str):
        super().__init__(f"Refused (HTTP 409): {reason}")
        self.reason = reason


MAX_ARTIFACT_BYTES = 65536
MAX_CONTEXT_JSON_BYTES = 128 * 1024


def segment(value: str) -> str:
    if not value or len(value) > 256 or value in {".", ".."}:
        raise ControlError("Invalid record identifier")
    return quote(value, safe="")


class ControlClient:
    def __init__(self, config: Config, transport=None):
        self.config = config
        self.transport = transport

    async def _request(
        self,
        method,
        path,
        *,
        body=None,
        key=None,
        max_bytes=2 * 1024 * 1024,
        relay_refusal=False,
        raise_refusal=False,
    ):
        headers = {
            "Authorization": "Bearer " + self.config.token(),
            "X-Agent-Destination": self.config.destination,
        }
        if key is not None:
            if not 1 <= len(key) <= 128 or any(ord(c) < 33 or ord(c) > 126 for c in key):
                raise ControlError("Idempotency key must be 1–128 printable non-space characters")
            headers["Idempotency-Key"] = key
        try:
            async with httpx.AsyncClient(
                base_url=self.config.endpoint,
                timeout=20,
                trust_env=False,
                follow_redirects=False,
                transport=self.transport,
                verify=ssl.create_default_context(cafile=str(self.config.ca_file))
                if self.config.ca_file
                else True,
            ) as client:
                async with client.stream(method, path, json=body, headers=headers) as response:
                    if response.status_code == 409 and raise_refusal:
                        raw = await response.aread()
                        try:
                            reason = json.loads(raw[:8192]).get("reason")
                        except (ValueError, AttributeError):
                            reason = None
                        if isinstance(reason, str) and reason:
                            raise ControlRefusal(reason[:100])
                    if response.status_code == 409 and relay_refusal:
                        # Only for routes whose refusal body is {status, reason} operator text
                        # (provider registration): the model needs the reason to fix its request.
                        raw = await response.aread()
                        try:
                            reason = json.loads(raw[:8192]).get("reason")
                        except (ValueError, AttributeError):
                            reason = None
                        if isinstance(reason, str):
                            raise ControlError(f"Refused (HTTP 409): {reason[:500]}")
                    if response.status_code not in {200, 201}:
                        # Never load/relay denial bodies, which could contain protected content.
                        raise ControlError(
                            f"Control API denied or rejected request (HTTP {response.status_code})"
                        )
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(content) + len(chunk) > max_bytes:
                            raise ControlError(
                                "Control response exceeds adapter serialization bound"
                            )
                        content.extend(chunk)
            return json.loads(content)
        except (httpx.HTTPError, ValueError):
            raise ControlError(
                "Control API unavailable or invalid response; no automatic retry"
            ) from None

    async def status(self):
        return await self._request("GET", "/external-agent/status")

    async def contexts(self, offset=0, limit=20):
        result = await self._request(
            "GET",
            self._collection("agent-contexts", offset, limit),
            max_bytes=MAX_CONTEXT_JSON_BYTES * limit,
        )
        for context in result["contexts"]:
            self._validate_context(context)
        return result

    async def context(self, context_id):
        result = await self._request(
            "GET", "/agent-contexts/" + segment(context_id), max_bytes=MAX_CONTEXT_JSON_BYTES
        )
        self._validate_context(result)
        return result

    @staticmethod
    def _validate_context(context):
        """Defense-in-depth output bound, not a substitute for backend disclosure authority."""
        if len(json.dumps(context).encode()) > MAX_CONTEXT_JSON_BYTES:
            raise ControlError("Context exceeds adapter serialization bound")
        if "content_base64" not in context:
            return
        encoded = context["content_base64"]
        try:
            if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_ARTIFACT_BYTES + 2) // 3):
                raise ValueError
            content = base64.b64decode(encoded, validate=True)
            if (
                len(content) > MAX_ARTIFACT_BYTES
                or context.get("size") != len(content)
                or context.get("sha256") != hashlib.sha256(content).hexdigest()
            ):
                raise ValueError
        except ValueError:
            raise ControlError("Invalid or oversized disclosed artifact context") from None

    async def proposals(self, offset=0, limit=20):
        return await self._request("GET", self._collection("agent-proposals", offset, limit))

    async def proposal(self, proposal_id):
        return await self._request("GET", "/agent-proposals/" + segment(proposal_id))

    async def propose(self, plan, provenance, idempotency_key):
        return await self._request(
            "POST",
            f"/projects/{segment(self.config.project_id)}/agent-proposals",
            body={"proposal_type": "task_plan", "plan": plan, "provenance": provenance},
            key=idempotency_key,
        )

    async def submit_campaign(self, spec, idempotency_key, predecessors=None):
        body = {"spec": spec}
        if predecessors:
            body["predecessors"] = dict(predecessors)
        return await self._request(
            "POST",
            f"/projects/{segment(self.config.project_id)}/campaigns",
            body=body,
            key=idempotency_key,
        )

    async def storage_retention_status(self):
        return await self._request("GET", "/storage-retention/status")

    async def storage_retention_plan(self):
        return await self._request("POST", "/storage-retention/plan", body={})

    async def storage_retention_apply(self, plan_digest):
        return await self._request(
            "POST",
            "/storage-retention/apply",
            body={"plan_digest": plan_digest},
            relay_refusal=True,
        )

    async def list_campaign_providers(self):
        return await self._request("GET", "/admin/campaign-providers")

    async def deploy_check_report(self):
        return await self._request("GET", "/admin/deploy-check")

    async def register_campaign_provider(self, body, idempotency_key):
        return await self._request(
            "POST",
            "/admin/campaign-providers",
            body=body,
            key=idempotency_key,
            relay_refusal=True,
        )

    async def model_spend(self, project_id=None, days=7):
        """Model-spend totals for a project (read only; limits are set by a person)."""
        if type(days) is not int or not 1 <= days <= 90:
            raise ControlError("days must be an integer from 1 to 90")
        project = segment(project_id or self.config.project_id)
        return await self._request("GET", f"/model-spend?project_id={project}&days={days}")

    async def resolve_model_spend(self, call_id, outcome, actual_cents, reason, idempotency_key):
        """Close an open model-spend reservation as not_sent or spent (a person's credential)."""
        if outcome not in ("not_sent", "spent"):
            raise ControlError("outcome must be not_sent or spent")
        if (outcome == "spent") != (actual_cents is not None):
            raise ControlError("actual_cents is required for, and only for, outcome spent")
        if actual_cents is not None and (
            type(actual_cents) not in (int, float) or not 0 <= actual_cents <= 10**9
        ):
            raise ControlError("actual_cents must be a number of cents from 0 up")
        if not isinstance(reason, str) or not 1 <= len(reason) <= 500:
            raise ControlError("reason must be 1 to 500 characters")
        body = {"outcome": outcome, "reason": reason}
        if actual_cents is not None:
            body["actual_cents"] = actual_cents
        return await self._request(
            "POST",
            f"/model-spend/reservations/{segment(call_id)}/resolution",
            body=body,
            key=idempotency_key,
        )

    async def request_agent_loop_pause(self, project, body, key):
        """POST the agent-loop pause request; `project` is an already-escaped path segment."""
        return await self._request(
            "POST", f"/projects/{project}/agent-loop/pause", body=body, key=key
        )

    async def provider_trust(self):
        return await self._request("GET", "/admin/provider-trust")

    async def credential_health(self):
        return await self._request("GET", "/credential-health")

    async def campaign(self, campaign_id):
        return await self._request("GET", "/campaigns/" + segment(campaign_id))

    async def campaign_report(self, campaign_id, detail=False):
        suffix = "?detail=true" if detail else ""
        return await self._request("GET", f"/campaigns/{segment(campaign_id)}/report{suffix}")

    async def campaign_window_report(self, since, until, detail=False, limit=100):
        if not 1 <= limit <= 200:
            raise ControlError("limit must be 1–200")
        project = segment(self.config.project_id)
        query = urlencode(
            {
                "since": since,
                "until": until,
                "limit": limit,
                **({"detail": "true"} if detail else {}),
            }
        )
        return await self._request("GET", f"/projects/{project}/campaign-report?{query}")

    async def list_campaigns(self, offset=0, limit=50):
        if not 0 <= offset or not 1 <= limit <= 200:
            raise ControlError("Pagination requires offset >= 0 and limit 1–200")
        project = segment(self.config.project_id)
        return await self._request(
            "GET", f"/projects/{project}/campaigns?offset={offset}&limit={limit}"
        )

    STUCK_STATUSES = ("open", "acknowledged", "resolved", "all")

    async def list_stuck(self, status="open", limit=50):
        """Stuck-campaign findings of this project (campaign:read); read only."""
        if status not in self.STUCK_STATUSES or not 1 <= limit <= 200:
            raise ControlError("Stuck findings need a known status and limit 1–200")
        project = segment(self.config.project_id)
        return await self._request(
            "GET", f"/projects/{project}/campaign-stuck?status={status}&limit={limit}"
        )

    async def scan_stuck(self, idempotency_key):
        """Run the platform's detector now (detections are recorded on receipts)."""
        project = segment(self.config.project_id)
        return await self._request(
            "POST", f"/projects/{project}/campaign-stuck/scan", body={}, key=idempotency_key
        )

    async def acknowledge_stuck(self, finding_id, note, idempotency_key):
        """Mark one finding seen (campaign:cancel); the campaign itself is untouched."""
        if not isinstance(note, str) or len(note) > 500:
            raise ControlError("A note is at most 500 characters")
        project = segment(self.config.project_id)
        return await self._request(
            "POST",
            f"/projects/{project}/campaign-stuck/{segment(finding_id)}/acknowledge",
            body={"note": note},
            key=idempotency_key,
        )

    async def campaign_receipt(self, campaign_id):
        return await self._request("GET", f"/campaigns/{segment(campaign_id)}/receipt")

    async def list_snapshots(self, offset=0, limit=50):
        if not 0 <= offset or not 1 <= limit <= 200:
            raise ControlError("Pagination requires offset >= 0 and limit 1–200")
        project = segment(self.config.project_id)
        return await self._request(
            "GET", f"/projects/{project}/agent-snapshots?offset={offset}&limit={limit}"
        )

    async def campaign_view(self, campaign_id, view):
        if view not in {"outputs", "logs", "budget", "grants", "retries", "holdout"}:
            raise ControlError("Unknown campaign view")
        return await self._request("GET", f"/campaigns/{segment(campaign_id)}/{view}")

    async def holdout_claims(self, partition_digest=None):
        project = segment(self.config.project_id)
        query = "" if partition_digest is None else f"?partition_digest={segment(partition_digest)}"
        return await self._request("GET", f"/projects/{project}/holdout-claims{query}")

    async def standing_status(self):
        project = segment(self.config.project_id)
        return await self._request("GET", f"/projects/{project}/agent-standing-status")

    async def project_spend(self):
        project = segment(self.config.project_id)
        return await self._request("GET", f"/projects/{project}/agent-spend")

    async def gate_decisions(self, campaign_id):
        return await self._request("GET", f"/campaigns/{segment(campaign_id)}/gate-decisions")

    async def cancel_campaign(self, campaign_id, expected_revision, reason, idempotency_key):
        return await self._request(
            "POST",
            f"/campaigns/{segment(campaign_id)}/cancel",
            body={"expected_revision": expected_revision, "reason": reason},
            key=idempotency_key,
        )

    async def validate_campaign_spec(self, spec, predecessors=None):
        """Dry-run of submit on the control plane: same checks, nothing stored."""
        body = {"spec": spec}
        if predecessors:
            body["predecessors"] = dict(predecessors)
        return await self._request(
            "POST",
            f"/projects/{segment(self.config.project_id)}/campaign-specs/validate",
            body=body,
        )

    async def reduce_campaign_budget(
        self, campaign_id, expected_revision, budget_cap_cents, reason, idempotency_key
    ):
        return await self._request(
            "POST",
            f"/campaigns/{segment(campaign_id)}/budget-reduction",
            body={
                "expected_revision": expected_revision,
                "budget_cap_cents": budget_cap_cents,
                "reason": reason,
            },
            key=idempotency_key,
        )

    async def rerun_campaign(
        self, campaign_id, idempotency_key, mode="failed_stages", stage_keys=None, cap=None
    ):
        body: dict = {"mode": mode}
        if stage_keys is not None:
            body["stage_keys"] = list(stage_keys)
        if cap is not None:
            body["budget_cap_cents"] = cap
        return await self._request(
            "POST", f"/campaigns/{segment(campaign_id)}/rerun", body=body, key=idempotency_key
        )

    async def _steer(self, campaign_id, action, body, key):
        return await self._request(
            "POST", f"/campaigns/{segment(campaign_id)}/{action}", body=body, key=key
        )

    async def pause_campaign(self, campaign_id, expected_revision, reason, idempotency_key):
        body = {"expected_revision": expected_revision, "reason": reason}
        return await self._steer(campaign_id, "pause", body, idempotency_key)

    async def resume_campaign(self, campaign_id, expected_revision, reason, idempotency_key):
        body = {"expected_revision": expected_revision, "reason": reason}
        return await self._steer(campaign_id, "resume", body, idempotency_key)

    async def limit_campaign_concurrency(
        self, campaign_id, expected_revision, max_concurrent_stages, reason, idempotency_key
    ):
        body = {
            "expected_revision": expected_revision,
            "max_concurrent_stages": max_concurrent_stages,
            "reason": reason,
        }
        return await self._steer(campaign_id, "concurrency-limit", body, idempotency_key)

    async def cancel_campaign_stage(
        self, campaign_id, stage_key, expected_revision, reason, idempotency_key
    ):
        body = {"expected_revision": expected_revision, "reason": reason}
        return await self._steer(
            campaign_id, f"stages/{segment(stage_key)}/cancel", body, idempotency_key
        )

    async def steer_campaign_placement(
        self, campaign_id, expected_revision, stage_key, pin, exclude, reason, idempotency_key
    ):
        return await self._request(
            "POST",
            f"/campaigns/{segment(campaign_id)}/placement-steering",
            body={
                "expected_revision": expected_revision,
                "stage_key": stage_key,
                "pin": pin,
                "exclude": list(exclude),
                "reason": reason,
            },
            key=idempotency_key,
        )

    async def list_pending_submissions(self, project_id, offset=0, limit=50):
        if not 0 <= offset or not 1 <= limit <= 100:
            raise ControlError("Pagination requires offset >= 0 and limit 1–100")
        return await self._request(
            "GET",
            f"/projects/{segment(project_id)}/submission-approvals"
            f"?status=pending&offset={offset}&limit={limit}",
        )

    async def read_submission(self, approval_id):
        return await self._request("GET", f"/submission-approvals/{segment(approval_id)}")

    async def cancel_submission(self, approval_id, expected_revision, reason, idempotency_key):
        return await self._request(
            "POST",
            f"/submission-approvals/{segment(approval_id)}/cancel",
            body={"expected_revision": expected_revision, "reason": reason},
            key=idempotency_key,
        )

    async def submit_cleared_campaign(self, approval_id, expected_revision, idempotency_key):
        return await self._request(
            "POST",
            f"/submission-approvals/{segment(approval_id)}/submit",
            body={"expected_revision": expected_revision},
            key=idempotency_key,
            raise_refusal=True,
        )

    async def approve_campaign(
        self, campaign_id, expected_revision, expected_spec_digest, idempotency_key
    ):
        return await self._request(
            "POST",
            f"/campaigns/{segment(campaign_id)}/approve",
            body={
                "expected_revision": expected_revision,
                "expected_spec_digest": expected_spec_digest,
            },
            key=idempotency_key,
        )

    async def revoke_campaign_release(
        self, campaign_id, release_id, expected_revision, reason, idempotency_key
    ):
        return await self._request(
            "POST",
            f"/campaigns/{segment(campaign_id)}/releases/{segment(release_id)}/revoke",
            body={"expected_revision": expected_revision, "reason": reason},
            key=idempotency_key,
        )

    def _collection(self, name, offset, limit):
        if not 0 <= offset or not 1 <= limit <= 100:
            raise ControlError("Pagination requires offset >= 0 and limit 1–100")
        return f"/projects/{segment(self.config.project_id)}/{name}?offset={offset}&limit={limit}"


class SubmissionActionClient:
    """Synchronous adapter giving CampaignSubmitAction list_pending/submit over a ControlClient.

    It exposes no approve, create or widen call.
    """

    def __init__(self, client: ControlClient, refused=None):
        """``refused(reason)`` builds the exception for a refusal (SubmitRefused by default)."""
        self._client = client
        self._refused = refused

    def list_pending(self, project_id):
        import asyncio

        return asyncio.run(self._client.list_pending_submissions(project_id, 0, 100))

    def submit(self, approval_id, expected_revision, idempotency_key):
        import asyncio

        refused = self._refused
        if refused is None:
            from tessera_agent.campaign_submit_action import SubmitRefused as refused
        try:
            return asyncio.run(
                self._client.submit_cleared_campaign(
                    approval_id, expected_revision, idempotency_key
                )
            )
        except ControlRefusal as refusal:
            raise refused(refusal.reason) from None
