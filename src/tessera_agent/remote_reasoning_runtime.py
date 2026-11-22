"""Standalone supervised reasoning, separate from the coordinator.

Two runtimes: the default direct Anthropic Messages API client (API key read only
from the manager credential directory, per-call receipts, per-call/daily caps) and
the optional operator-pinned OpenCode V2 harness for OpenAI included OAuth. No
permission replies, shell execution, human approval or generic HTTP tool proxy.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import math
import os
import re
import signal
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request

from tessera_worker.opencode_http import OpenCodeHttpClient, _default_open

from .anthropic_messages import AnthropicMessagesReasoning, validate_anthropic_profile
from .model_destinations import (
    ACCOUNT_MODES,
    MODEL_ID,
    RUNTIME_ANTHROPIC_MESSAGES,
    TIER_UNCLASSIFIED,
    DestinationRefused,
    destination,
    model_tier,
    require_tier_admitted,
    validate_credential_ref,
)
from .model_input_guard import SealedInputRefused, refuse_sealed_inputs
from .provenance_client import ProvenanceClient
from .remote_reasoning_store import RATE_LIMIT_CONDITIONS, ReasoningStore, private_file, utc_day

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class RuntimeBlocked(Exception):
    """Sanitized fail-closed failure; never includes upstream bodies."""


class ModelConditionBlocked(RuntimeBlocked):
    """A sanitized model rate-limit/quota condition; carries only a numeric hint."""

    def __init__(self, condition: str, retry_after: float | None = None):
        super().__init__(condition)
        self.retry_after = retry_after


_MAX_BACKOFF_SECONDS = 24 * 3600
_DEFAULT_BACKOFF_SECONDS = {"model_rate_limited": 60, "model_quota_exhausted": 3600}
_SHA256 = re.compile(r"[a-f0-9]{64}\Z")
# Definite rejections of one prompt are retried after backoff at most this often.
MAX_PROMPT_ATTEMPTS = 8
# Sanitized provenance error code for a request that was definitely not accepted.
_NOT_SENT_ERRORS = {
    "model_rate_limited": "model_rate_limited",
    "model_quota_exhausted": "model_quota_exhausted",
    "model_call_cap_exceeded": "budget_exceeded",
    "daily_cap_exceeded": "budget_exceeded",
    "model_egress_refused": "permission_denied",
}
# Halt reasons kept as-is for the Anthropic runtime even though the effect is uncertain.
_UNCERTAIN_KEPT = frozenset({"model_substituted", "model_call_failed", "model_output_unrecorded"})


def _retry_after(value) -> float | None:
    """Numeric seconds only (never an HTTP-date or upstream text), bounded."""
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,6}(\.[0-9]{1,3})?", value):
        value = float(value)
    if type(value) in {int, float} and math.isfinite(value) and 0 < value <= _MAX_BACKOFF_SECONDS:
        return float(value)
    return None


def _session_condition(error) -> tuple[str, float | None] | None:
    """Detect a limit from allowlisted error class fields only; never message text."""
    if isinstance(error, str):
        fields, hint = [error], None
    elif isinstance(error, dict):
        fields = [error.get(k) for k in ("name", "class", "type", "code", "status")]
        hint = _retry_after(error.get("retry_after"))
    else:
        return None
    text = " ".join(str(f).lower() for f in fields if isinstance(f, (str, int)))
    if any(m in text for m in ("quota", "insufficient", "usage_limit", "billing")):
        return "model_quota_exhausted", hint
    if any(m in text for m in ("rate_limit", "ratelimit", "rate-limit", "too_many", "429")):
        return "model_rate_limited", hint
    return None


class JsonEndpoint:
    def __init__(
        self,
        origin: str,
        token: str,
        destination: str | None = None,
        *,
        auth_mode="bearer",
        model_conditions=False,
    ):
        # Only the model harness endpoint maps 429/402 to a model condition; a
        # control-plane 429 is not a statement about the model.
        self.model_conditions = bool(model_conditions)
        self.origin = OpenCodeHttpClient._origin(origin)
        if not token or any(ord(c) < 32 or ord(c) == 127 for c in token):
            raise ValueError("Invalid supplied endpoint credential.")
        if auth_mode not in {"bearer", "basic"}:
            raise ValueError("Unsupported endpoint authentication mode.")
        authorization = "Bearer " + token
        if auth_mode == "basic":
            authorization = "Basic " + base64.b64encode(("opencode:" + token).encode()).decode()
        self.headers = {"Authorization": authorization, "Content-Type": "application/json"}
        if destination:
            self.headers["X-Agent-Destination"] = destination

    def call(self, method: str, path: str, body: dict | None = None):
        request = Request(
            self.origin + path,
            data=None if body is None else json.dumps(body, allow_nan=False).encode(),
            headers=self.headers,
            method=method,
        )
        try:
            response = _default_open(request, 5)
            try:
                raw = response.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    raise RuntimeBlocked("response_bound")
                return json.loads(raw)
            finally:
                response.close()
        except HTTPError as error:
            # Status code and a numeric Retry-After only; the body is never read.
            code = error.code
            hint = _retry_after(error.headers.get("Retry-After")) if error.headers else None
            if self.model_conditions and code in {402, 429}:
                raise ModelConditionBlocked(
                    "model_quota_exhausted" if code == 402 else "model_rate_limited", hint
                ) from None
            raise RuntimeBlocked("endpoint_unavailable_or_invalid") from None
        except Exception:
            raise RuntimeBlocked("endpoint_unavailable_or_invalid") from None


class OpenCodeV2Reasoning:
    """Real HTTP adapter for the V2 documented create/prompt/get/interrupt seam."""

    def __init__(self, endpoint: JsonEndpoint, profile: dict):
        self.endpoint, self.profile = endpoint, profile

    def verify(self):
        document = self.endpoint.call("GET", "/openapi.json")
        digest = hashlib.sha256(
            json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        info = self.endpoint.call("GET", "/api/info")
        if digest != self.profile["schema_sha256"] or info["version"] != self.profile["version"]:
            raise RuntimeBlocked("harness_pin_changed")
        agent = self.endpoint.call("GET", "/api/agent/" + self.profile["agent"])["data"]
        if agent.get("steps") != self.profile["max_steps"]:
            raise RuntimeBlocked("harness_step_bound_changed")
        if agent.get("model") != self.profile["model"]:
            raise RuntimeBlocked("harness_model_changed")
        if (
            agent.get("permissions", [])[-len(self.profile["permissions"]) :]
            != self.profile["permissions"]
        ):
            raise RuntimeBlocked("harness_permissions_changed")
        return True

    def create(self, row: dict):
        result = self.endpoint.call(
            "POST",
            "/api/session",
            {
                "id": row["session_id"],
                "agent": self.profile["agent"],
                "model": self.profile["model"],
                "location": {"directory": self.profile["workspace"]},
                "permissions": self.profile["permissions"],
                "metadata": {"tessera_run": row["id"], "binding_digest": row["digest"]},
            },
        )["data"]
        if result.get("id") != row["session_id"]:
            raise RuntimeBlocked("session_identity_mismatch")

    def prompt(self, row: dict, text: str):
        if hasattr(self, "prompt_fence"):
            self.prompt_fence(row, text)
        self.endpoint.call(
            "POST",
            f"/api/session/{row['session_id']}/prompt",
            {
                "id": row["message_id"],
                "text": text,
                "resume": True,
                "metadata": {"tessera_run": row["id"]},
            },
        )

    def status(self, row: dict) -> dict:
        data = self.endpoint.call("GET", f"/api/session/{row['session_id']}")["data"]
        if (
            data.get("id") != row["session_id"]
            or data.get("metadata", {}).get("binding_digest") != row["digest"]
            or data.get("model") != self.profile["model"]
            or data.get("permissions") != self.profile["permissions"]
        ):
            raise RuntimeBlocked("session_binding_changed")
        usage = data["tokens"]
        tokens = sum(usage[k] for k in ("input", "output", "reasoning"))
        tokens += sum(usage["cache"][k] for k in ("read", "write"))
        cost = data["cost"]
        if (
            not math.isfinite(tokens)
            or tokens < 0
            or int(tokens) != tokens
            or not math.isfinite(cost)
            or cost < 0
        ):
            raise RuntimeBlocked("usage_unknown")
        observation = {
            "tokens": int(tokens),
            "cost": cost,
            "outcome": data.get("outcome"),
            # Harness-reported, unverified; configured identity is the binding above.
            "usage": {
                "input": usage["input"],
                "output": usage["output"],
                "reasoning": usage["reasoning"],
                "cache_read": usage["cache"]["read"],
                "cache_write": usage["cache"]["write"],
            },
        }
        reported = data.get("model")
        if isinstance(reported, dict) and all(
            isinstance(reported.get(k), str) for k in ("providerID", "id")
        ):
            observation["reported_model"] = reported["providerID"] + "/" + reported["id"]
        found = _session_condition(data.get("error"))
        if found:
            observation["rate_limit"] = {"condition": found[0], "retry_after": found[1]}
        return observation

    def interrupt(self, row: dict):
        # Cleanup is allowed after authority expiry; never resumes parked input.
        self.endpoint.call("POST", f"/api/session/{row['session_id']}/interrupt?resume=false")


class ReasoningSupervisor:
    def __init__(
        self,
        store,
        harness,
        authority,
        profile,
        clock=time.time,
        public_capture=None,
        holdout_lookup=None,
    ):
        self.store, self.harness, self.authority = store, harness, authority
        self.profile, self.clock = profile, clock
        self.public_capture = public_capture
        # lookup(project_id, digest) -> control holdout-claims body (ADR 0068).
        self.holdout_lookup = holdout_lookup
        found = ACCOUNT_MODES.get(profile.get("account_mode"))
        # Billed destinations (Anthropic Messages API): per-call receipts, computed cost
        # in cents, per-call/daily/grant caps and definite-rejection retry semantics.
        self.billed = bool(found and found.billed)
        self.direct_api = bool(found and found.runtime == RUNTIME_ANTHROPIC_MESSAGES)
        self._grant: dict | None = None
        self._last_usage: dict[str, dict] = {}
        self._pending_output: dict[str, str] = {}
        if self.billed:
            for name, hook in (
                ("budget_check", self.budget_check),
                ("output_sink", self.output_sink),
            ):
                if getattr(harness, name, False) is None:
                    setattr(harness, name, hook)

    def fence(self, row: dict):
        binding = json.loads(row["binding"])
        if row["stop_requested"]:
            raise RuntimeBlocked("stop_requested")
        if self.clock() >= row["started"] + self.profile["max_duration_seconds"]:
            raise RuntimeBlocked("duration_expired")
        if row["tokens"] >= self.profile["max_tokens"] or (not self.billed and row["cost"] > 0):
            raise RuntimeBlocked("usage_fence")
        grant = self.authority(binding["grant_id"])
        self._grant = grant
        bounds = grant["bounds"]
        expiry = datetime.fromisoformat(bounds["expires_at"].replace("Z", "+00:00"))
        if expiry.tzinfo is None or datetime.fromtimestamp(self.clock(), UTC) >= expiry:
            raise RuntimeBlocked("grant_expired")
        for field in ("revision", "epoch"):
            if grant[field] != binding[field]:
                raise RuntimeBlocked("grant_revision_changed")
        if grant["status"] != "active" or grant.get("effective_status") != "active":
            raise RuntimeBlocked("grant_revoked")
        for field in ("subject_credential_id", "destination", "model"):
            if bounds[field] != binding[field]:
                raise RuntimeBlocked("grant_scope_changed")
        if bounds["budget_cents"] <= 0 or grant.get("remaining_budget_cents", 0) <= 0:
            raise RuntimeBlocked("budget_exhausted")
        if self.billed:
            # Billed cost (cents, computed from pinned prices) must fit the grant budget.
            if row["cost"] > grant["remaining_budget_cents"]:
                raise RuntimeBlocked("budget_exhausted")
            self._daily_fence()
        if self.profile["max_duration_seconds"] > bounds["max_duration_seconds"]:
            raise RuntimeBlocked("duration_outside_grant")
        self.store.permission_current(row["id"])

    @staticmethod
    def policy(profile: dict) -> dict:
        """Canonical admitted policy subset. Never includes credential references."""
        model = profile.get("model")
        provider = model.get("providerID") if isinstance(model, dict) else None
        model_id = model.get("id") if isinstance(model, dict) else None
        return {
            "account_mode": profile.get("account_mode"),
            "provider": provider,
            "model": f"{provider}/{model_id}" if provider and model_id else model_id,
            "tier": model_tier(provider or "", model_id or ""),
            "usage_bounds": {
                k: profile.get(k) for k in ("max_steps", "max_tokens", "max_duration_seconds")
            },
            "permissions": profile.get("permissions"),
            "input_class": profile.get("input_class", DEFAULT_INPUT_CLASS),
        } | {
            # Billed destinations only: caps and operator-pinned prices are policy.
            k: profile[k]
            for k in ("limits", "pricing_cents_per_mtok")
            if k in profile
        }

    # -- billed caps -----------------------------------------------------------------------
    def _daily(self) -> dict:
        return self.store.daily_usage(utc_day(self.clock()))

    def _daily_fence(self):
        limits, day = self.profile["limits"], self._daily()
        if (
            day["tokens"] > limits["daily_token_cap"]
            or day["cost_cents"] > limits["daily_cost_cap_cents"]
        ):
            raise RuntimeBlocked("daily_cap_exceeded")

    def budget_check(self, worst_tokens: int, worst_cents: float) -> bool:
        """Preflight for one billed call: run, daily (UTC) and grant caps, worst case."""
        limits = self.profile["limits"]
        row = self.store.get(self.profile["run_id"])
        day = self._daily()
        grant = self._grant
        if grant is None or worst_cents > limits["max_cost_cents_per_call"]:
            return False
        if (
            day["tokens"] + worst_tokens > limits["daily_token_cap"]
            or day["cost_cents"] + worst_cents > limits["daily_cost_cap_cents"]
            or row["tokens"] + worst_tokens > self.profile["max_tokens"]
            or row["cost"] + worst_cents > grant.get("remaining_budget_cents", 0)
        ):
            return False
        return True

    def output_sink(self, row: dict, text: str):
        """Write completion text to a private file; only its digest reaches the receipt."""
        data = text.encode()
        outputs = self.store.directory / "outputs"
        outputs.mkdir(mode=0o700, exist_ok=True)
        if outputs.lstat().st_mode & 0o077 or outputs.is_symlink():
            raise RuntimeBlocked("output_directory_not_private")
        seq = self.store.next_model_call_seq(row["id"])
        fd = os.open(
            outputs / f"{row['id']}-{seq}.txt",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view) :]
            os.fsync(fd)
        finally:
            os.close(fd)
        self._pending_output[row["id"]] = hashlib.sha256(data).hexdigest()

    def guard_inputs(self, run_id: str):
        """Refuse sealed-holdout inputs before the prompt (ADR 0068); fail closed."""
        declared = self._declared_digests()
        project = self.profile.get("project_id")
        if not declared and not project:
            return
        if (
            not isinstance(project, str)
            or not _ID.fullmatch(project)
            or self.holdout_lookup is None
        ):
            raise RuntimeBlocked("model_input_seal_unknown")
        try:
            refuse_sealed_inputs(
                [*declared.values(), self.store.input_digest(run_id)],
                lambda digest: self.holdout_lookup(project, digest),
            )
        except SealedInputRefused as error:
            raise RuntimeBlocked(str(error)) from None

    def _declared_digests(self) -> dict:
        declared = self.profile.get("input_digests", {})
        if not isinstance(declared, dict) or any(
            not isinstance(k, str) or not isinstance(v, str) or not _SHA256.fullmatch(v)
            for k, v in declared.items()
        ):
            raise RuntimeBlocked("input_digest_invalid")
        return declared

    def _record_billed_failure(self, row: dict, billed_call: dict):
        """Receipt a billed call that ends the run uncertain, and charge the run and the day.

        Usage reported by the response is recorded as is; otherwise the adapter's reserved
        preflight worst case, marked ``reserved_worst_case_estimate``. Either way it counts
        toward the UTC-day ledger and the run cost the grant-budget fence reads.
        """
        tokens, cost, usage = billed_call["tokens"], billed_call["cost"], billed_call["usage"]
        if (
            type(tokens) is not int
            or tokens < 0
            or type(cost) not in {int, float}
            or not math.isfinite(cost)
            or cost < 0
            or not isinstance(usage, dict)
        ):
            raise RuntimeBlocked("usage_unknown")
        previous = self.store.get(row["id"])
        before = self._last_usage.get(row["id"], {})
        observation = {
            "tokens": previous["tokens"] + tokens,
            "cost": previous["cost"] + cost,
            "usage": {k: before.get(k, 0) + v for k, v in usage.items()},
            "reported_model": billed_call.get("reported_model"),
            "cost_cents_estimate_max": billed_call.get("cost_cents_estimate_max"),
        }
        self.store.usage(row["id"], observation["tokens"], observation["cost"])
        self._record_call(
            self.store.get(row["id"]),
            observation,
            previous,
            cost_basis=billed_call["cost_basis"],
            outcome_class=billed_call["outcome_class"],
        )

    def _record_call(
        self,
        row: dict,
        observation: dict,
        previous: dict,
        *,
        cost_basis: str | None = None,
        outcome_class: str | None = None,
    ):
        """Append one receipt row. ``previous`` is the run's usage before this observation.

        ``cost_basis``/``outcome_class`` mark a billed call that halts the run (model
        substituted, output unrecorded, outcome uncertain); that run halts anyway, so the
        per-call cap is not re-raised for it.
        """
        binding = json.loads(row["binding"])
        policy = self.policy(self.profile)
        declared = self._declared_digests()
        tokens_delta = observation["tokens"] - previous["tokens"]
        cost_delta = observation["cost"] - previous["cost"]
        detail = observation.get("usage")
        if self.billed:
            # Per-call receipt: the delta between cumulative status() snapshots.
            before = self._last_usage.get(row["id"], {})
            usage = (
                {k: v - before.get(k, 0) for k, v in detail.items()}
                if isinstance(detail, dict)
                else None
            )
            if isinstance(detail, dict):
                self._last_usage[row["id"]] = dict(detail)
            tokens = {**(usage or {}), "total": tokens_delta}
            cost = cost_delta
        else:
            tokens = (
                {**detail, "total": observation["tokens"]}
                if isinstance(detail, dict)
                else {"total": observation["tokens"]}
            )
            cost = observation["cost"]
        reported = observation.get("reported_model")
        receipt = {
            "model_configured": policy["model"],
            "model_reported": reported if isinstance(reported, str) else None,
            "model_identity_basis": "provider_response_model"
            if self.direct_api
            else "harness_reported_unverified",
            "tier": policy["tier"],
            "prompt_sha256": binding.get("prompt_sha256"),
            "profile_sha256": binding.get("profile_sha256"),
            "input_sha256": self.store.input_digest(row["id"]),
            "input_digests": dict(sorted(declared.items())),
            "input_class": policy["input_class"],
            "policy_sha256": hashlib.sha256(
                json.dumps(policy, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
            ).hexdigest(),
            "tokens": tokens,
            "cost": cost,
            "cost_basis": "computed_from_pinned_prices"
            if self.billed
            else "included_subscription_usage"
            if cost == 0 and self.profile.get("account_mode") in ACCOUNT_MODES
            else "reported_cost",
        }
        now = self.clock()
        if self.billed:
            day = self.store.daily_usage(utc_day(now))
            receipt.update(
                limits=dict(self.profile["limits"]),
                daily_tokens_after=day["tokens"] + tokens_delta,
                daily_cost_cents_after=day["cost_cents"] + cost_delta,
                cost_cents_estimate_max=observation.get("cost_cents_estimate_max"),
                output_sha256=self._pending_output.pop(row["id"], None),
            )
            if cost_basis is not None:
                receipt["cost_basis"] = cost_basis
            if outcome_class is not None:
                receipt["outcome_class"] = outcome_class
        self.store.model_call(
            row["id"],
            receipt,
            now,
            tokens_delta=max(tokens_delta, 0),
            cost_cents_delta=max(cost_delta, 0.0) if self.billed else 0.0,
            only_if_changed=not self.billed,
        )
        if (
            self.billed
            and outcome_class is None
            and cost_delta > self.profile["limits"]["max_cost_cents_per_call"]
        ):
            raise RuntimeBlocked("model_call_cap_exceeded")

    def _back_off(self, run_id: str, condition: str, retry_after=None):
        """Record a limit and pause. This never changes the model, tier or profile."""
        if condition not in RATE_LIMIT_CONDITIONS:
            raise RuntimeBlocked("invalid_authority_or_usage")
        wait = _retry_after(retry_after) or _DEFAULT_BACKOFF_SECONDS[condition]
        now = self.clock()
        self.store.rate_limit(run_id, condition, now + wait, now)

    def _backing_off(self, run_id: str) -> bool:
        limit = self.store.latest_rate_limit(run_id)
        return limit is not None and self.clock() < limit["backoff_until"]

    def effect(self, row, boundary, callback):
        previous = self.store.effect(row["id"], boundary)
        if previous == "acknowledged":
            return
        tool = "anthropic.messages." if self.direct_api else "opencode.session."
        if (
            self.direct_api
            and previous == "not_sent"
            and self.store.attempts(row["id"], boundary) >= MAX_PROMPT_ATTEMPTS
        ):
            raise RuntimeBlocked("model_retry_exhausted")
        if not self.store.begin(row["id"], boundary, tool):
            raise RuntimeBlocked("uncertain_" + boundary)
        try:
            callback()
        except Exception as error:
            code = str(error) if isinstance(error, RuntimeBlocked) else None
            if self.direct_api and getattr(error, "request_sent", None) is False:
                # Definite rejection: nothing was accepted upstream. The intent is
                # recorded as not sent; the caller backs off (limits) or halts (caps).
                self.store.finish(
                    row["id"],
                    boundary,
                    "not_sent",
                    error=_NOT_SENT_ERRORS.get(code, "provider_failure"),
                    tool_prefix=tool,
                )
                if isinstance(error, RuntimeBlocked):
                    raise
                raise RuntimeBlocked("model_call_failed") from None
            # Fail closed: a limited intent is still uncertain and never replayed.
            if code in RATE_LIMIT_CONDITIONS:
                self._back_off(row["id"], code, getattr(error, "retry_after", None))
            billed_call = getattr(error, "billed_call", None)
            try:
                if self.direct_api and isinstance(billed_call, dict):
                    # Billed or possibly billed: receipt it before the intent is closed.
                    self._record_billed_failure(row, billed_call)
            finally:
                self.store.finish(row["id"], boundary, "uncertain", tool_prefix=tool)
            if self.direct_api and code in _UNCERTAIN_KEPT:
                raise RuntimeBlocked(code) from None
            raise RuntimeBlocked("uncertain_" + boundary) from None
        self.store.finish(row["id"], boundary, "acknowledged", tool_prefix=tool)

    def halt(self, row: dict, reason: str):
        self.store.state(row["id"], "stopping", reason)
        try:
            if self.store.effect(row["id"], "create"):
                self.effect(row, "interrupt", lambda: self.harness.interrupt(row))
        except RuntimeBlocked:
            self.store.state(row["id"], "stop_unknown", reason)
        else:
            # Acknowledgement does not establish provider termination.
            self.store.state(row["id"], "stop_acknowledged", reason)

    def tick(self, run_id: str, text: str | None = None) -> dict:
        row = self.store.get(run_id)
        if row["state"] in {"completed", "failed", "stop_acknowledged", "stop_unknown"}:
            return row
        try:
            self.fence(row)
            if self._backing_off(run_id):
                return self.store.get(run_id)  # No harness call, no new prompt.
            self.harness.verify()
            self.fence(self.store.get(run_id))
            self.effect(row, "create", lambda: self.harness.create(row))
            self.fence(self.store.get(run_id))
            if self.store.effect(run_id, "prompt") != "acknowledged":
                if text is None or hashlib.sha256(
                    text.encode()
                ).hexdigest() != self.store.input_digest(run_id):
                    raise RuntimeBlocked("prompt_required_or_changed")
                self.guard_inputs(run_id)
                self.effect(row, "prompt", lambda: self.harness.prompt(row, text))
            observation = self.harness.status(row)
            previous = self.store.get(run_id)
            self.store.usage(run_id, observation["tokens"], observation["cost"])
            self._record_call(self.store.get(run_id), observation, previous)
            limit = observation.get("rate_limit")
            if limit:
                self._back_off(run_id, limit["condition"], limit.get("retry_after"))
            self.fence(self.store.get(run_id))
            if self.public_capture is not None:
                # Only a separately admitted normalized producer may supply events.
                # Session usage, prompt text and private parts are not capture input.
                self.public_capture.after_status(self.store.get(run_id), observation)
                self.fence(self.store.get(run_id))
            outcome = observation["outcome"]
            self.store.state(
                run_id,
                "running"
                if limit
                else "completed"
                if outcome == "succeeded"
                else "failed"
                if outcome
                else "running",
            )
        except RuntimeBlocked as error:
            if str(error) not in RATE_LIMIT_CONDITIONS:
                self.halt(row, str(error))
            else:
                self._back_off(run_id, str(error), getattr(error, "retry_after", None))
                if self.store.effect(run_id, "prompt") == "acknowledged":
                    self.store.state(run_id, "running")
        except Exception as error:
            reason = (
                str(error) if isinstance(error, RuntimeBlocked) else "invalid_authority_or_usage"
            )
            self.halt(row, reason)
        return self.store.get(run_id)


INPUT_CLASSES = frozenset({"public", "private_code_approved", "private_derived"})
DEFAULT_INPUT_CLASS = "private_derived"  # ADR 0039: undeclared input is private.


def validate_profile(profile: dict, *, skill_id=None):
    if "input_class" in profile and not (
        isinstance(profile["input_class"], str) and profile["input_class"] in INPUT_CLASSES
    ):
        raise ValueError("Invalid input data class.")
    try:
        found = destination(profile["model"]["providerID"], profile["account_mode"])
    except (DestinationRefused, KeyError, TypeError):
        found = None
    direct_api = found is not None and found.runtime == RUNTIME_ANTHROPIC_MESSAGES
    # The direct Messages API runtime has no harness agent; OpenCode profiles name one.
    fields = ("run_id", "grant_id", "subject_credential_id")
    if not direct_api or "agent" in profile:
        fields += ("agent",)
    for field in fields:
        if not isinstance(profile.get(field), str) or not _ID.fullmatch(profile[field]):
            raise ValueError("Invalid runtime identifier.")
    try:
        if found is None:
            raise DestinationRefused("unenrolled_model_destination")
        validate_credential_ref(found, profile.get("credential_ref"))
        model_id = profile["model"].get("id")
        # OpenAI research profiles may omit the id (pinned by the harness binding);
        # an Anthropic profile must name one so its policy tier is known.
        if (model_id is not None or found.provider_id == "anthropic") and not (
            isinstance(model_id, str) and MODEL_ID.fullmatch(model_id)
        ):
            raise DestinationRefused("unenrolled_model_identifier")
        if found.provider_id == "anthropic":
            tier = model_tier("anthropic", profile["model"]["id"])
            if tier == TIER_UNCLASSIFIED:
                raise DestinationRefused("unclassified_model_tier")
            require_tier_admitted(tier, profile.get("fable_approval_id"))
    except DestinationRefused:
        raise ValueError(
            "Only an operator-attested OpenAI included-OAuth harness or Anthropic"
            " API-key-reference destination is supported."
        ) from None
    if any("api_key" in str(key).lower() for key in profile):
        raise ValueError("API-key fallback is not a supported account mode.")
    for field, maximum in (
        ("max_steps", 16),
        ("max_tokens", 32768),
        ("max_duration_seconds", 3600),
    ):
        if type(profile[field]) is not int or not 0 < profile[field] <= maximum:
            raise ValueError("Invalid runtime usage bound.")
    if direct_api:
        # No tool use in this runtime yet: permissions must be deny-only; limits and
        # operator-pinned prices are required (codes only, never values).
        try:
            validate_anthropic_profile(profile)
        except ValueError as error:
            raise ValueError(f"Invalid Anthropic runtime profile ({error}).") from None
        return
    rules = profile["permissions"]
    if not rules or rules[0] != {"action": "*", "resource": "*", "effect": "deny"}:
        raise ValueError("Harness must deny ambient tool permissions.")
    for rule in rules[1:]:
        if skill_id is not None and rule == {
            "action": "skill",
            "resource": skill_id,
            "effect": "allow",
        }:
            continue  # Only the exact separately pinned curated research skill.
        if (
            rule["effect"] != "allow"
            or not re.fullmatch(r"tessera_[A-Za-z0-9_]+", rule["action"])
            or rule["resource"] != "*"
        ):
            raise ValueError("Only exact destination-scoped Tessera MCP tools may be allowed.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "status", "stop", "report"))
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--prompt-file", type=Path)
    args = parser.parse_args(argv)
    profile = json.loads(private_file(args.profile))
    validate_profile(profile)
    found = destination(profile["model"]["providerID"], profile["account_mode"])
    credentials_dir = None
    if args.command == "run" and found.runtime == RUNTIME_ANTHROPIC_MESSAGES:
        # The API key is read only from the manager-provided credential directory.
        value = os.environ.get("CREDENTIALS_DIRECTORY", "")
        if not value or not Path(value).is_absolute():
            raise RuntimeBlocked("missing_manager_credential_directory")
        credentials_dir = Path(value)
    store = ReasoningStore(args.state_dir)
    lock = None
    try:
        if args.command != "run":
            if args.command == "stop":
                store.stop(profile["run_id"])
            if args.command == "report":
                with ProvenanceClient(
                    profile["control_url"],
                    os.environ[profile["agent_token_env"]],
                    profile["destination"],
                ) as collector:
                    store.drain_provenance(profile["run_id"], collector, profile["project_id"])
            row = store.get(profile["run_id"])
        else:
            lock = os.open(
                args.state_dir / "supervisor.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
            )
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            text = private_file(args.prompt_file).decode() if args.prompt_file else None
            if text is not None and (not text.strip() or len(text.encode()) > 8192):
                raise ValueError("Prompt must contain 1..8192 bytes.")
            binding = {
                k: profile[k]
                for k in (
                    "grant_id",
                    "revision",
                    "epoch",
                    "subject_credential_id",
                    "destination",
                )
            }
            binding["model"] = profile["model"]["id"]
            binding["prompt_sha256"] = profile["prompt_sha256"]
            binding["profile_sha256"] = hashlib.sha256(private_file(args.profile)).hexdigest()
            store.bind(profile["run_id"], binding, time.time())
            control = JsonEndpoint(
                profile["control_url"],
                os.environ[profile["agent_token_env"]],
                profile["destination"],
            )
            if credentials_dir is not None:
                harness = AnthropicMessagesReasoning(profile, credentials_dir=credentials_dir)
            else:
                harness = OpenCodeV2Reasoning(
                    JsonEndpoint(
                        profile["harness_url"],
                        os.environ[profile["harness_token_env"]],
                        auth_mode=profile.get("harness_auth_mode", "basic"),
                        model_conditions=True,
                    ),
                    profile,
                )
            supervisor = ReasoningSupervisor(
                store,
                harness,
                lambda gid: control.call("GET", "/agent-authority-grants/" + gid),
                profile,
                holdout_lookup=lambda project, digest: control.call(
                    "GET",
                    f"/projects/{project}/holdout-claims?partition_digest={digest}",
                ),
            )
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda *_: store.stop(profile["run_id"]))
            while True:
                row = supervisor.tick(profile["run_id"], text)
                if row["state"] not in {"ready", "running"}:
                    break
                time.sleep(1)
            if profile.get("project_id"):
                with ProvenanceClient(
                    profile["control_url"],
                    os.environ[profile["agent_token_env"]],
                    profile["destination"],
                    timeout=1,
                ) as collector:
                    store.drain_provenance(profile["run_id"], collector, profile["project_id"])
        receipt = {k: row[k] for k in ("id", "state", "reason", "tokens", "cost")}
        receipt["provenance"] = store.provenance_coverage(profile["run_id"])
        receipt["model_calls"] = store.model_calls(profile["run_id"])
        receipt["rate_limit"] = store.latest_rate_limit(profile["run_id"])
        print(json.dumps(receipt))
        if args.command == "report":
            return 0 if receipt["provenance"]["delivery_complete"] else 2
        return 0 if row["state"] == "completed" else 2
    finally:
        store.close()
        if lock is not None:
            os.close(lock)


if __name__ == "__main__":
    raise SystemExit(main())
