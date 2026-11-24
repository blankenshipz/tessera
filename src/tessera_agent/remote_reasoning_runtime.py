"""Standalone supervised reasoning, separate from the coordinator.

Two runtimes: the default direct Anthropic Messages API client (API key read only
from the manager credential directory, per-call receipts, per-call/daily caps) and
the optional operator-pinned OpenCode V2 harness for OpenAI included OAuth. The direct
runtime may run a multi-turn loop over an exact allowlist of Tessera MCP tools (deny by
default); every model turn and every tool call is receipted by digest. No permission
replies, shell execution, human approval or generic HTTP tool proxy.

Exit codes of ``run`` (a one-shot supervisor: each run identity runs once):

- 0 the run completed, or the run identity was already completed before this start (a closed
  line is logged; nothing is sent, reserved or rerun).
- 1 an unexpected crash (Python's default); the journal makes a restart resume without replay.
- 2 the run ended without completing: a fence or block halted it (its code is the receipt's
  ``reason``) or it was stopped; also an already-closed run identity that did not complete.
- 77 refused: another supervisor holds the state directory, or the run identity is bound to a
  different profile/prompt (a new run needs a new run identity).
- 78 configuration error: unreadable or invalid profile or prompt, missing manager credential
  directory or agent-token variable.

``report`` exits 0 when provenance delivery is complete and 2 otherwise.
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
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request

from tessera_worker.opencode_http import OpenCodeHttpClient, _default_open

from .anthropic_messages import (
    AnthropicMessagesReasoning,
    CapRefused,
    allowed_tools,
    validate_anthropic_profile,
)
from .mcp_tool_bridge import McpToolBridge, ToolBridgeBlocked, catalog_sha256
from .model_destinations import (
    ACCOUNT_MODES,
    MODEL_ID,
    RUNTIME_ANTHROPIC_MESSAGES,
    TIER_UNCLASSIFIED,
    DestinationRefused,
    destination,
    model_tier,
    refuse_api_key_environment,
    require_tier_admitted,
    validate_credential_ref,
)
from .model_input_guard import SealedInputRefused, refuse_sealed_inputs
from .model_spend_client import CAP_EXCEEDED, GRANT_CODES, NOT_CONFIGURED, SpendLedger
from .provenance_client import ProvenanceClient
from .remote_reasoning_store import (
    ANSWERED_TOOL_STATUSES,
    RATE_LIMIT_CONDITIONS,
    DailyCapRefused,
    ReasoningStore,
    manager_credentials_dir,
    private_file,
    utc_day,
)

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")

EXIT_COMPLETED = 0
EXIT_CRASH = 1  # Never returned: an uncaught exception exits 1.
EXIT_BLOCKED = 2
EXIT_REFUSED = 77
EXIT_CONFIG = 78
# A run in one of these states is closed: a later start never resumes or reruns it.
CLOSED_STATES = frozenset({"completed", "failed", "stop_acknowledged", "stop_unknown"})


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
_UNCERTAIN_KEPT = frozenset(
    {
        "model_substituted",
        "model_call_failed",
        "model_output_unrecorded",
        "tool_call_cap_exceeded",
        "tool_call_uncertain",
        "tool_catalog_changed",
        "tool_call_unrecorded",
        "model_call_unrecorded",
        "model_resume_unavailable",
        "model_resume_state_invalid",
        "model_park_unrecorded",
    }
)
_REASON = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


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

    def call(self, method: str, path: str, body: dict | None = None, key: str | None = None):
        headers = self.headers
        if key is not None:
            if not 1 <= len(key) <= 128 or any(ord(c) < 33 or ord(c) > 126 for c in key):
                raise RuntimeBlocked("endpoint_unavailable_or_invalid")
            headers = {**headers, "Idempotency-Key": key}
        request = Request(
            self.origin + path,
            data=None if body is None else json.dumps(body, allow_nan=False).encode(),
            headers=headers,
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
        spend=None,
    ):
        self.store, self.harness, self.authority = store, harness, authority
        # Model-spend ledger port (control plane); None keeps the local-only caps (tests, and
        # destinations that are not billed).
        self.spend = spend
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
        # Per-turn loop state (direct Messages API runtime only; in memory, never replayed).
        self._reserved: dict[str, int] = {}  # seq of the in-flight call's pending reservation
        self._final_seq: dict[str, int] = {}  # final turn's settled row awaiting its output digest
        self._halts: dict[str, str] = {}  # supervisor fence code raised inside the loop
        self._per_turn: set[str] = set()  # runs receipted per turn by on_model_call
        self._resume_pending: set[str] = set()  # runs whose loop may be resumed after backoff
        self._open_tool_turn: dict[str, int] = {}  # turn whose tool receipts await provenance
        # The one strict parser (bad rules are refused); only the direct runtime has tools.
        self._allowed_tools = allowed_tools(profile) if self.direct_api else frozenset()
        if self.billed:
            for name, hook in (
                ("budget_check", self.budget_check),
                ("output_sink", self.output_sink),
                ("on_model_call", self.on_model_call),
                ("on_model_reserve", self.on_model_reserve),
                ("on_model_release", self.on_model_release),
                ("on_tool_call", self.on_tool_call),
                ("on_tool_intent", self.on_tool_intent),
                ("on_tool_result", self.on_tool_result),
                ("between_turns", self.between_turns),
                ("before_tool", self.before_tool),
                ("on_park", self.on_park),
                ("on_unpark", self.on_unpark),
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
        self._kind_fence(bounds)
        if bounds["budget_cents"] <= 0 or grant.get("remaining_budget_cents", 0) <= 0:
            raise RuntimeBlocked("budget_exhausted")
        if self.billed:
            # Billed cost (cents, computed from pinned prices) must fit the grant budget.
            # With the ledger on, the grant's remaining budget already has this run's settled and
            # open calls taken out of it (ADR 0142); without it only this run's cost is known.
            if self._own_cost(row) > grant["remaining_budget_cents"]:
                raise RuntimeBlocked("budget_exhausted")
            self._daily_fence()
            if self.spend is not None and self._spend_project() is None:
                raise RuntimeBlocked(NOT_CONFIGURED)
        if self.profile["max_duration_seconds"] > bounds["max_duration_seconds"]:
            raise RuntimeBlocked("duration_outside_grant")
        self.store.permission_current(row["id"])

    def _kind_fence(self, bounds: dict):
        """Execution grants keep their contract. A reasoning grant (model calls only) must
        name this profile's provider, carry exactly ``model:call`` and no binding, and any tool
        the model may call must be one the live server marks read-only: the tools run under
        the agent credential's own scopes, which the grant cannot narrow server-side."""
        kind = bounds.get("kind", "execution")
        if kind == "execution":
            return
        if kind != "reasoning":
            raise RuntimeBlocked("grant_kind_unsupported")
        model = self.profile.get("model")
        provider = model.get("providerID") if isinstance(model, dict) else None
        if (
            bounds.get("actions") != ["model:call"]
            or bounds.get("bindings")
            or not provider
            or bounds.get("provider_id") != provider
        ):
            raise RuntimeBlocked("grant_scope_changed")
        if self._allowed_tools:
            bridge = getattr(self.harness, "tool_bridge", None)
            reader = getattr(bridge, "read_only_tools", None)
            try:
                read_only = reader() if callable(reader) else frozenset()
            except Exception:  # noqa: BLE001 - fail closed with a code only
                read_only = frozenset()
            if not self._allowed_tools <= read_only:
                raise RuntimeBlocked("tool_not_read_only")

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
            # Billed destinations only: caps, operator-pinned prices and the tool catalog pin.
            k: profile[k]
            for k in (
                "limits",
                "pricing_cents_per_mtok",
                "substitution_pricing_cents_per_mtok",
                "tool_catalog_sha256",
            )
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

    def _own_cost(self, row: dict) -> float:
        """This run's cost to count against the grant's remaining budget: none when the control
        plane's ledger debits the grant itself (the remaining budget already includes it)."""
        return 0 if self.spend is not None else row["cost"]

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
            or self._own_cost(row) + worst_cents > grant.get("remaining_budget_cents", 0)
        ):
            return False
        if self.spend is not None:
            return self._reserve(row["id"], worst_cents)
        return True

    # -- model-spend ledger (control plane) ------------------------------------------------
    def _spend_project(self) -> str | None:
        """The project whose model-spend limits apply: the grant's, or the profile's declared
        one. Both set and different, or neither, is unconfigured (fail closed)."""
        declared = (self.profile.get("model_spend") or {}).get("project_id")
        granted = (self._grant or {}).get("project_id")
        if declared and granted and declared != granted:
            return None
        return granted or declared or None

    def _reserve(self, run_id: str, worst_cents: float) -> bool:
        """Reserve the call's worst case before anything is sent. A refusal never changes the
        model: the call is simply not made. The ledger being unreachable is a refusal too."""
        project = self._spend_project()
        if project is None:
            raise RuntimeBlocked(NOT_CONFIGURED)
        if self.store.spend_open(run_id) is not None:
            raise RuntimeBlocked("model_spend_reservation_open")
        seq = self.store.spend_next_seq(run_id)
        reservation = self.spend.reserve(
            run_id, seq, worst_cents, project, (self._grant or {}).get("id")
        )
        if reservation.allowed:
            self.store.spend_reserved(run_id, seq, reservation.call_id, worst_cents)
            return True
        if reservation.code in (CAP_EXCEEDED, NOT_CONFIGURED, *GRANT_CODES):
            # A definite refusal: record it so the next attempt gets a new key (the ledger
            # replays a refusal for the same key forever). An unreachable ledger is not recorded.
            self.store.spend_denied(run_id, seq)
        if reservation.code in (CAP_EXCEEDED, *GRANT_CODES):
            return False  # the client refuses: model_call_cap_exceeded, nothing sent
        if reservation.code == NOT_CONFIGURED:
            raise RuntimeBlocked(NOT_CONFIGURED)
        raise RuntimeBlocked("model_spend_unavailable")

    def _release_open(self, run_id: str):
        """A definitely-not-sent call gives its reservation back. If the release is not
        acknowledged the reservation stays open and keeps counting against the caps."""
        open_row = self.store.spend_open(run_id)
        if open_row is None or self.spend is None:
            return
        if self.spend.release(open_row["call_id"], run_id, open_row["seq"]):
            self.store.spend_resolve(run_id, open_row["seq"], "released")

    def output_sink(self, row: dict, text: str):
        """Write completion text to a private file; only its digest reaches the receipt."""
        try:
            self._write_output(row, text)
        except Exception:
            # The final turn's receipt (settled by on_model_call, actual usage) stays; mark it
            # so the row says the completion was not kept.
            seq = self._final_seq.pop(row["id"], None)
            if seq is not None:
                try:
                    self.store.annotate_model_call(
                        row["id"], seq, {"outcome_class": "output_unrecorded"}
                    )
                except Exception:  # noqa: BLE001 - never mask the sink's own failure
                    pass
            raise

    def _write_output(self, row: dict, text: str):
        data = text.encode()
        outputs = self.store.directory / "outputs"
        outputs.mkdir(mode=0o700, exist_ok=True)
        if outputs.lstat().st_mode & 0o077 or outputs.is_symlink():
            raise RuntimeBlocked("output_directory_not_private")
        # The final turn's receipt row (already settled), else the row about to be written.
        final = self._final_seq.get(row["id"])
        seq = final if final is not None else self.store.next_model_call_seq(row["id"])
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
        digest = hashlib.sha256(data).hexdigest()
        if final is None:
            self._pending_output[row["id"]] = digest
            return
        self.store.annotate_model_call(row["id"], final, {"output_sha256": digest})
        self._final_seq.pop(row["id"], None)

    # -- per-turn loop hooks (direct Messages API runtime) ---------------------------------
    def _halt_in_loop(self, run_id: str, code: str):
        """Remember a supervisor-side stop so effect() keeps its reason, then raise it."""
        self._halts[run_id] = code
        error = RuntimeBlocked(code)
        error.request_sent = None
        raise error

    def between_turns(self, row: dict):
        """Kill switch between turns: stop request, duration, grant, budget, daily cap."""
        self._tool_turn_in_loop(row["id"])  # The previous turn's tool rows are all terminal.
        self._fence_in_loop(row)

    def _fence_in_loop(self, row: dict):
        try:
            self.fence(self.store.get(row["id"]))
        except RuntimeBlocked as error:
            self._halt_in_loop(row["id"], str(error))
        except Exception:  # noqa: BLE001 - fail closed with a code only
            self._halt_in_loop(row["id"], "invalid_authority_or_usage")

    def on_model_call(self, row: dict, call: dict):
        """Append one receipt row for this turn and update run usage before the next turn."""
        run_id = row["id"]
        try:
            usage = call["usage"]
            counts = {
                k: usage[k] for k in ("input", "output", "reasoning", "cache_read", "cache_write")
            }
            total, cost = call["total"], call["cost"]
            turn, uses = call["turn"], call["tool_use_count"]
            if (
                any(type(v) is not int or v < 0 for v in [*counts.values(), total, turn, uses])
                or type(cost) not in {int, float}
                or not math.isfinite(cost)
                or cost < 0
            ):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            self._halt_in_loop(run_id, "usage_unknown")
        unrun = call.get("unrun_tool_count", 0)
        if type(unrun) is not int or not 0 <= unrun <= uses:
            self._halt_in_loop(run_id, "usage_unknown")
        self._tool_turn_in_loop(run_id)
        stop_reason = call.get("stop_reason")
        reported = call.get("reported_model")
        estimate = call.get("cost_cents_estimate_max")
        self._per_turn.add(run_id)
        receipt = self._receipt(self.store.get(run_id), reported, {**counts, "total": total}, cost)
        receipt.update(
            limits=dict(self.profile["limits"]),
            cost_cents_estimate_max=estimate
            if type(estimate) in {int, float} and math.isfinite(estimate)
            else None,
            turn=turn,
            stop_reason=stop_reason
            if isinstance(stop_reason, str) and _REASON.fullmatch(stop_reason)
            else None,
            tool_use_count=uses,
        )
        if "unrun_tool_count" in call:
            receipt["unrun_tool_count"] = unrun
        receipt["output_sha256"] = None
        try:
            seq = self._settle(run_id, receipt, total, float(cost))
        except RuntimeBlocked as error:
            # The turn's row is kept; the ledger refused or could not take the settlement.
            self._halt_in_loop(run_id, str(error))
        if stop_reason != "tool_use":
            # The final turn: its output digest is added once the completion is written.
            self._final_seq[run_id] = seq
        if cost > self.profile["limits"]["max_cost_cents_per_call"]:
            self._halt_in_loop(run_id, "model_call_cap_exceeded")

    def on_model_reserve(self, row: dict, reservation: dict):
        """Before a billed request: durably reserve its worst case (ADR 0083 §5).

        One transaction writes a ``pending`` receipt row holding the counted input plus the
        output cap and ``cost_cents_estimate_max``, and adds it to the run totals and the
        UTC-day ledger, so every later preflight (this run or another) sees it. The daily
        caps are checked in that same transaction (``budget_check`` is only the early check);
        a refusal raises ``CapRefused("daily_cap_exceeded")``. Any failure here means the
        request is not sent.
        """
        run_id = row["id"]
        usage, tokens, cost = reservation["usage"], reservation["tokens"], reservation["cost"]
        if (
            not isinstance(usage, dict)
            or type(tokens) is not int
            or tokens < 0
            or type(cost) not in {int, float}
            or not math.isfinite(cost)
            or cost < 0
        ):
            raise RuntimeBlocked("usage_unknown")
        if self._reserved.get(run_id) is not None:
            raise RuntimeBlocked("model_call_unrecorded")  # one in-flight call per run
        receipt = self._receipt(self.store.get(run_id), None, {**usage, "total": tokens}, cost)
        now = self.clock()
        limits = self.profile["limits"]
        receipt.update(
            limits=dict(limits),
            cost_cents_estimate_max=reservation.get("cost_cents_estimate_max"),
            output_sha256=None,
            cost_basis="reserved_worst_case_estimate",
            outcome_class="uncertain",
            **self._billed_turn_fields(reservation),
        )
        held = self.store.spend_open(run_id) if self.spend is not None else None
        if held is not None:
            # The control-plane reservation (made in budget_check) stays open with this
            # call: if the call turns out uncertain, a person resolves it.
            receipt["spend_ledger"] = {"call_id": held["call_id"], "status": "reserved"}
        # The authoritative daily-cap check: the store re-reads the day's ledger (every run in
        # this state dir) under the write lock and inserts the row in the same transaction.
        try:
            self._reserved[run_id] = self.store.reserve_model_call(
                run_id,
                receipt,
                now,
                tokens_delta=tokens,
                cost_cents_delta=float(cost),
                daily_token_cap=limits["daily_token_cap"],
                daily_cost_cap_cents=limits["daily_cost_cap_cents"],
            )
        except DailyCapRefused:
            raise CapRefused("daily_cap_exceeded") from None

    def on_model_release(self, row: dict):
        """A definite rejection sent nothing billable: void the pending reservation.

        The reservation is dropped from memory only after the void commits. If the void
        fails, it stays held: ``effect()`` counts the call as progress (never ``not_sent``),
        its pending row becomes the uncertain receipt, and the adapter halts the run with
        ``model_call_unrecorded`` (outcome unknown), so the call is never resent.
        """
        seq = self._reserved.get(row["id"])
        if seq is not None:
            self.store.void_model_call(row["id"], seq, self.clock())
            self._reserved.pop(row["id"], None)
        # Nothing billable was sent and the void committed: the control plane's reservation
        # is given back too. A failed void raised above, so a possibly sent call keeps it open.
        self._release_open(row["id"])

    def _held(self, run_id: str) -> dict | None:
        """The in-flight call's pending reservation (tokens, cents, ledger day), if any."""
        seq = self._reserved.get(run_id)
        return None if seq is None else self.store.model_call_reservation(run_id, seq)

    def _settle(self, run_id: str, receipt: dict, tokens_delta: int, cost_delta: float) -> int:
        """Write one billed call's receipt with its run totals and ledger entry: one transaction.

        With a pending reservation the row is settled in place (run totals and ledger move by
        the difference); otherwise a settled row is appended (``record_model_call``).
        ``daily_*_after`` is completed here.
        """
        held = self._held(run_id)
        now = self.clock()
        day = self.store.daily_usage(held["day"] if held else utc_day(now))
        reserved_tokens, reserved_cents = (held["tokens"], held["cost_cents"]) if held else (0, 0.0)
        receipt.update(
            daily_tokens_after=day["tokens"] - reserved_tokens + tokens_delta,
            daily_cost_cents_after=day["cost_cents"] - reserved_cents + cost_delta,
        )
        settlement, blocked = None, None
        if self.spend is not None:
            # The same cents settle the control plane's project/destination reservation.
            settlement, blocked = self._settle_ledger(
                run_id, receipt, max(cost_delta, 0.0), receipt.get("outcome_class")
            )
        seq = self._write_settlement(run_id, receipt, now, held, tokens_delta, cost_delta)
        if blocked is not None:
            raise RuntimeBlocked(blocked)  # the row above is kept; the ledger was not updated
        if settlement is not None and settlement.cap_exceeded:
            raise RuntimeBlocked("daily_cap_exceeded")
        return seq

    def _write_settlement(self, run_id, receipt, now, held, tokens_delta, cost_delta) -> int:
        if held is not None:
            seq = self._reserved[run_id]
            self.store.settle_model_call(
                run_id, seq, receipt, now, tokens_delta=tokens_delta, cost_cents_delta=cost_delta
            )
            self._reserved.pop(run_id, None)
            return seq
        current = self.store.get(run_id)
        return self.store.record_model_call(
            run_id,
            receipt,
            now,
            run_tokens=current["tokens"] + tokens_delta,
            run_cost=current["cost"] + cost_delta,
            tokens_delta=tokens_delta,
            cost_cents_delta=cost_delta,
        )

    def _reservation_stands(self, run_id: str):
        """The call's settlement was not written: its reservation becomes the receipt."""
        if self._reserved.pop(run_id, None) is None:
            return
        try:
            self.store.model_calls_interrupted(run_id, self.clock())
        except Exception:  # noqa: BLE001 - the pending row is settled on the next tick
            pass

    def before_tool(self, row: dict):
        """Kill switch before every executed tool call: the same fence as between turns.

        The turn's tool provenance is not flushed here: later rows of the turn are pending.
        """
        self._fence_in_loop(row)

    def _tool_name(self, call) -> str:
        name = call.get("name") if isinstance(call, dict) else None
        return name if name in self._allowed_tools else "unlisted"

    def on_tool_intent(self, row: dict, call: dict):
        """Journal an allowed tool call as started before the bridge runs it; the handle."""
        record = {
            k: call.get(k) if isinstance(call, dict) else None
            for k in ("turn", "index", "input_sha256", "allowed")
        }
        record["name"] = self._tool_name(call)
        run_id = row["id"]
        self._note_tool_turn(run_id, record["turn"])
        try:
            if record["name"] == "unlisted":
                raise ValueError
            return run_id, self.store.tool_call_begin(run_id, record, self.clock())
        except ValueError:
            self._halt_in_loop(run_id, "tool_call_unrecorded")

    def on_tool_result(self, handle, outcome: dict):
        """Complete the started row exactly once: succeeded, error or uncertain."""
        run_id, seq = handle
        try:
            if not isinstance(outcome, dict):
                raise ValueError
            self.store.tool_call_finish(
                run_id,
                seq,
                outcome.get("status"),
                self.clock(),
                result_sha256=outcome.get("result_sha256"),
                is_error=outcome.get("is_error"),
                truncated=outcome.get("truncated"),
            )
        except ValueError:
            self._halt_in_loop(run_id, "tool_call_unrecorded")

    def on_tool_call(self, row: dict, call: dict):
        """A never-executed tool's receipt (denied or refused); its name only if allowlisted."""
        name = self._tool_name(call)
        record = {
            k: call.get(k) if isinstance(call, dict) else None
            for k in (
                "turn",
                "index",
                "input_sha256",
                "result_sha256",
                "is_error",
                "truncated",
                "status",
                "skip_reason",
            )
        }
        record.update(name=name, allowed=name != "unlisted" and call.get("allowed") is True)
        run_id = row["id"]
        self._note_tool_turn(run_id, record["turn"])
        try:
            self.store.tool_call(run_id, record, self.clock())
        except ValueError:
            self._halt_in_loop(run_id, "tool_call_unrecorded")

    def _note_tool_turn(self, run_id: str, turn):
        """Track the turn whose tool rows await their aggregated provenance record."""
        if self._open_tool_turn.get(run_id) not in {None, turn}:
            self._tool_turn_in_loop(run_id)
        if type(turn) is int:
            self._open_tool_turn[run_id] = turn

    def _flush_tool_turn(self, run_id: str):
        """One aggregated provenance record for the finished turn's tool receipts."""
        turn = self._open_tool_turn.pop(run_id, None)
        if turn is not None:
            self.store.tool_turn(run_id, turn)

    def _tool_turn_in_loop(self, run_id: str):
        try:
            self._flush_tool_turn(run_id)
        except ValueError:
            self._halt_in_loop(run_id, "tool_call_unrecorded")

    # -- durable paused loop (direct Messages API runtime) ----------------------------------
    def on_park(self, row: dict, state: dict):
        """Persist a parked loop to a private file; the journal keeps only its digest."""
        run_id = row["id"]
        turn = state.get("turn") if isinstance(state, dict) else None
        if state.get("run_id") != run_id or type(turn) is not int or turn < 2:
            raise ValueError("Invalid parked loop state.")
        self._flush_tool_turn(run_id)
        data = json.dumps(state, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        digest = self.store.write_pending(run_id, data)
        self.store.loop_park(run_id, turn, digest, self.clock())

    def on_unpark(self, row: dict):
        """The loop completed or halted: its persisted state is obsolete."""
        self.store.discard_pending(row["id"])

    def _restore(self, row: dict, turn: int, park: dict | None):
        """Re-arm a parked loop from its private file after verifying it against the journal."""
        run_id = row["id"]
        try:
            if park is None or park["turn"] != turn:
                raise ValueError
            data = self.store.read_pending(run_id)
            if data is None or hashlib.sha256(data).hexdigest() != park["state_sha256"]:
                raise ValueError
            state = json.loads(data)
            totals = self.store.get(run_id)
            cumulative = state["cumulative"]
            if (
                state["run_id"] != run_id
                or state["turn"] != turn
                or type(cumulative["tokens"]) is not int
                or cumulative["tokens"] != totals["tokens"]
                or type(cumulative["cost"]) not in {int, float}
                or not math.isclose(cumulative["cost"], totals["cost"], rel_tol=1e-9, abs_tol=1e-9)
                or not self._tool_calls_match(run_id, state["tool_calls_used"])
            ):
                raise ValueError
            restore = self.harness.restore
        except Exception:  # noqa: BLE001 - fail closed with a code only
            raise RuntimeBlocked("model_resume_state_invalid") from None
        try:
            restore(row, state)
        except RuntimeBlocked as error:
            code = str(error)
            raise RuntimeBlocked(
                code if _REASON.fullmatch(code) else "model_resume_state_invalid"
            ) from None
        except Exception:  # noqa: BLE001
            raise RuntimeBlocked("model_resume_state_invalid") from None

    def _tool_calls_match(self, run_id: str, used) -> bool:
        """A parked loop's tool_calls_used is the journal's count of answered tool calls.

        Answered calls (a tool_result went back to the model and counted against
        max_tool_calls) are ``succeeded`` and ``error`` (executed) plus ``denied`` (never
        executed). A parked loop has no other rows: a started, uncertain or refused row
        means the loop halted, so it is not resumable.
        """
        statuses = [call["status"] for call in self.store.tool_calls(run_id)]
        return (
            type(used) is int
            and all(status in ANSWERED_TOOL_STATUSES for status in statuses)
            and used == len(statuses)
        )

    def _resume(self, row: dict, turn: int, from_disk: bool):
        run_id = row["id"]
        park = self.store.latest_loop_park(run_id)
        if from_disk:
            self._restore(row, turn, park)

        def resume():
            if park is not None and park["turn"] == turn:
                self.store.loop_resume(run_id, turn, park["state_sha256"], from_disk, self.clock())
            self.harness.resume(row)

        self.effect(row, f"resume.{turn}", resume)

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
        preflight worst case, marked ``reserved_worst_case_estimate`` (a substituted model is
        charged ``max(actual, reserved)``). Either way it counts
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
        held = self._held(row["id"])
        if held is not None:
            # The run totals hold this call's reservation; the charge replaces it.
            previous = dict(previous) | {
                "tokens": max(previous["tokens"] - held["tokens"], 0),
                "cost": max(previous["cost"] - held["cost_cents"], 0.0),
            }
        before = self._last_usage.get(row["id"], {})
        observation = {
            "tokens": previous["tokens"] + tokens,
            "cost": previous["cost"] + cost,
            "usage": {k: before.get(k, 0) + v for k, v in usage.items()},
            "reported_model": billed_call.get("reported_model"),
            "cost_cents_estimate_max": billed_call.get("cost_cents_estimate_max"),
        }
        # Billed: the run totals, receipt and daily ledger are one transaction (_record_call).
        self._record_call(
            previous,
            observation,
            previous,
            cost_basis=billed_call["cost_basis"],
            outcome_class=billed_call["outcome_class"],
            turn_fields=self._billed_turn_fields(billed_call),
        )

    @staticmethod
    def _billed_turn_fields(billed_call: dict) -> dict:
        """The tool loop's per-turn fields for a halting call, when the adapter gave them."""
        fields = {}
        turn = billed_call.get("turn")
        if type(turn) is int and turn > 0:
            fields["turn"] = turn
            stop_reason = billed_call.get("stop_reason")
            fields["stop_reason"] = (
                stop_reason
                if isinstance(stop_reason, str) and _REASON.fullmatch(stop_reason)
                else None
            )
        return fields

    def _record_call(
        self,
        row: dict,
        observation: dict,
        previous: dict,
        *,
        cost_basis: str | None = None,
        outcome_class: str | None = None,
        turn_fields: dict | None = None,
    ):
        """Append one receipt row. ``previous`` is the run's usage before this observation.

        Billed: the run's cumulative tokens/cost, the receipt and the daily-ledger entry are
        written in one store transaction (``record_model_call``).  Otherwise the caller has
        already stored the run usage and the receipt row is appended on its own.

        ``cost_basis``/``outcome_class`` mark a billed call that halts the run (model
        substituted, output unrecorded, outcome uncertain); that run halts anyway, so the
        per-call cap is not re-raised for it. ``turn_fields`` (``turn``, ``stop_reason``) place
        such a call in the tool loop: it is that turn's row.
        """
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
        receipt = self._receipt(row, observation.get("reported_model"), tokens, cost)
        if self.billed:
            receipt.update(
                limits=dict(self.profile["limits"]),
                cost_cents_estimate_max=observation.get("cost_cents_estimate_max"),
                output_sha256=self._pending_output.pop(row["id"], None),
            )
            if cost_basis is not None:
                receipt["cost_basis"] = cost_basis
            if outcome_class is not None:
                receipt["outcome_class"] = outcome_class
            if turn_fields:
                receipt.update(turn_fields)
            # Settles the call's pending reservation, or appends a row: run totals, receipt
            # and daily ledger in one transaction either way.
            self._settle(row["id"], receipt, max(tokens_delta, 0), max(cost_delta, 0.0))
        else:
            self.store.model_call(
                row["id"],
                receipt,
                self.clock(),
                tokens_delta=max(tokens_delta, 0),
                only_if_changed=True,
            )
        if (
            self.billed
            and outcome_class is None
            and cost_delta > self.profile["limits"]["max_cost_cents_per_call"]
        ):
            raise RuntimeBlocked("model_call_cap_exceeded")

    def _receipt(self, row: dict, reported, tokens, cost) -> dict:
        """The receipt fields shared by every model-call row. Never prompt or output text."""
        binding = json.loads(row["binding"])
        policy = self.policy(self.profile)
        declared = self._declared_digests()
        return {
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

    def _settle_ledger(self, run_id: str, receipt: dict, cost_cents: float, outcome_class=None):
        """Settle the call's cost into the control plane's ledger and mark the receipt.

        The digest binds the receipt as it stands without ``spend_ledger`` (the settlement
        answer is not known yet). On failure the reservation stays open and keeps counting
        against the caps, the receipt says ``unsettled``, and the run halts."""
        open_row = self.store.spend_open(run_id)
        if outcome_class == "uncertain" and open_row is not None:
            # The call may or may not have been billed: the receipt carries only the reserved
            # worst case. The reservation stays open (still counting) until a person resolves it.
            receipt["spend_ledger"] = {"call_id": open_row["call_id"], "status": "reserved"}
            return None, None
        if open_row is None:
            if cost_cents <= 0:
                return None, None  # a re-polled observation, not a new call
            receipt["spend_ledger"] = {"status": "unreserved"}
            return None, "model_spend_unavailable"
        digest = hashlib.sha256(
            json.dumps(receipt, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()
        settled = self.spend.settle(
            open_row["call_id"], run_id, open_row["seq"], cost_cents, digest
        )
        if not settled.ok:
            receipt["spend_ledger"] = {"call_id": open_row["call_id"], "status": "unsettled"}
            return None, "model_spend_unavailable"
        self.store.spend_resolve(
            run_id,
            open_row["seq"],
            "settled",
            settled_micro_cents=settled.settled_micro_cents,
        )
        receipt["spend_ledger"] = {
            "call_id": settled.call_id,
            "status": "settled",
            "settled_micro_cents": settled.settled_micro_cents,
            "over_reservation": settled.over_reservation,
            "cap_exceeded": settled.cap_exceeded,
        }
        return settled, None

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
        run_id = row["id"]
        turns_before = len(self.store.model_calls(run_id))
        self._halts.pop(run_id, None)
        try:
            try:
                callback()
            except BaseException:
                # A process death here leaves an in-flight call's reservation pending; the
                # next tick settles it as an uncertain receipt (model_calls_interrupted).
                self._final_seq.pop(run_id, None)
                # Never mask the loop's own failure: a tool-turn provenance write that
                # fails here leaves that turn journal-only (its tool_calls rows stay).
                try:
                    self._flush_tool_turn(run_id)
                except Exception:  # noqa: BLE001
                    pass
                raise
            else:
                self._final_seq.pop(run_id, None)
                self._flush_tool_turn(run_id)
        except Exception as error:
            code = str(error) if isinstance(error, RuntimeBlocked) else None
            # A reservation still held here was sent and neither settled nor released.
            reserved = self._reserved.get(run_id) is not None
            if not isinstance(getattr(error, "billed_call", None), dict):
                # A reservation neither settled nor released (its settlement or release
                # failed): the possibly billed call is receipted as its worst case. This is
                # converted before progress is judged (review rv-atu2 finding 1).
                self._reservation_stands(run_id)
            # Progress: a newly settled receipt, or a sent call whose reservation stands as
            # its receipt (even if converting it failed: the next tick settles it). A sent,
            # receipted call is never journaled as not_sent.
            progressed = reserved or len(self.store.model_calls(run_id)) > turns_before
            halt = self._halts.pop(run_id, None)
            sent = getattr(error, "request_sent", None)
            # Only a rate/quota condition is resumed after backoff; a resumable cap
            # refusal of a later turn halts like any other stop after accepted turns.
            resumable = getattr(error, "resumable", False) is True and code in RATE_LIMIT_CONDITIONS
            if self.direct_api and (halt is not None or (sent is True and not resumable)):
                # The loop stopped between turns (the supervisor's own fence, or a local
                # tool-loop stop after an accepted turn): every accepted turn is receipted;
                # no further turn was sent.
                halt = halt or (code if code and _REASON.fullmatch(code) else "model_call_failed")
                self.store.finish(
                    run_id,
                    boundary,
                    "acknowledged" if progressed else "not_sent",
                    error=None if progressed else _NOT_SENT_ERRORS.get(halt, "provider_failure"),
                    tool_prefix=tool,
                )
                raise RuntimeBlocked(halt) from None
            if self.direct_api and resumable and sent is True:
                # A definite rejection of a later turn: the earlier turns were accepted and
                # receipted; the rejected turn was not. The loop resumes after backoff at the
                # rejected turn (resume.N attempts are bounded like prompt attempts).
                rejected = _NOT_SENT_ERRORS.get(code, "provider_failure")
                self.store.finish(
                    run_id,
                    boundary,
                    "acknowledged" if progressed else "not_sent",
                    error=None if progressed else rejected,
                    tool_prefix=tool,
                )
                if progressed:
                    # The rejected turn's first attempt counts toward its resume cap.
                    resume = f"resume.{len(self.store.model_calls(run_id)) + 1}"
                    if self.store.begin(run_id, resume, tool):
                        self.store.finish(
                            run_id, resume, "not_sent", error=rejected, tool_prefix=tool
                        )
                self._resume_pending.add(run_id)
                if not reserved:  # a standing reservation was possibly sent: stays open
                    self._release_open(run_id)  # a definite rejection billed nothing
                if isinstance(error, RuntimeBlocked):
                    raise
                raise RuntimeBlocked("model_call_failed") from None
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
                if not reserved:  # a standing reservation was possibly sent: stays open
                    self._release_open(row["id"])
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
            except Exception:
                self._reservation_stands(run_id)
                raise
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
        # A tool row still started was cut off by a dead process: the call may have run.
        # It is marked uncertain and the run halts; the loop is never replayed.
        interrupted = self.store.tool_calls_interrupted(run_id, self.clock())
        # Likewise a billed call still reserved (pending): its reservation, already in the
        # run totals and the day's ledger, becomes its uncertain receipt; never resent.
        calls_interrupted = self.store.model_calls_interrupted(run_id, self.clock())
        self._reserved.pop(run_id, None)
        if row["state"] in CLOSED_STATES:
            return self.store.get(run_id) if calls_interrupted else row
        if interrupted or calls_interrupted:
            # Before any restore or resume: a parked loop's private file is deleted.
            self.store.discard_pending(run_id)
            self.halt(row, "tool_call_uncertain" if interrupted else "model_call_uncertain")
            return self.store.get(run_id)
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
            elif self.direct_api and (
                run_id in self._resume_pending
                or self.store.effect(run_id, f"resume.{len(self.store.model_calls(run_id)) + 1}")
                == "not_sent"
            ):
                # Continue the loop at the rejected turn; nothing is replayed. Without an
                # in-memory loop (a restart) the parked state is restored from its private
                # file once its digest and the run totals match the journal.
                from_disk = run_id not in self._resume_pending
                self._resume_pending.discard(run_id)
                self._resume(row, len(self.store.model_calls(run_id)) + 1, from_disk)
            observation = self.harness.status(row)
            if not (self.direct_api and run_id in self._per_turn):
                previous = self.store.get(run_id)
                if self.billed:
                    # Run totals, receipt and daily ledger in one transaction.
                    self._record_call(previous, observation, previous)
                else:
                    self.store.usage(run_id, observation["tokens"], observation["cost"])
                    self._record_call(self.store.get(run_id), observation, previous)
            # Per-turn runs were receipted and their usage recorded by on_model_call.
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
            reason = observation.get("reason")
            self.store.state(
                run_id,
                "running"
                if limit
                else "completed"
                if outcome == "succeeded"
                else "failed"
                if outcome
                else "running",
                reason
                if outcome
                and outcome != "succeeded"
                and isinstance(reason, str)
                and _REASON.fullmatch(reason)
                else None,
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
    if "model_spend" in profile:
        spend = profile["model_spend"]
        if (
            not isinstance(spend, dict)
            or set(spend) != {"project_id"}
            or not isinstance(spend["project_id"], str)
            or not _ID.fullmatch(spend["project_id"])
        ):
            raise ValueError("Invalid model-spend setting.")
    for field, maximum in (
        ("max_steps", 16),
        ("max_tokens", 32768),
        ("max_duration_seconds", 3600),
    ):
        if type(profile[field]) is not int or not 0 < profile[field] <= maximum:
            raise ValueError("Invalid runtime usage bound.")
    if direct_api:
        # Deny-all permissions plus exact tessera_* allows (with a catalog pin and tool
        # limits when any tool is allowed); limits and operator-pinned prices are
        # required (codes only, never values).
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


_CATALOG_PLACEHOLDER = "0" * 64


def _manager_credentials_dir() -> Path:
    value = os.environ.get("CREDENTIALS_DIRECTORY", "")
    if not value or not Path(value).is_absolute():
        raise RuntimeBlocked("missing_manager_credential_directory")
    return Path(value)


def build_tool_bridge(profile: dict, credentials_dir: Path):
    """Start the deny-by-default MCP bridge for the profile's exact tool allowlist."""
    bridge = McpToolBridge(
        allowed=allowed_tools(profile),
        control_url=profile["control_url"],
        project_id=profile["project_id"],
        destination=profile["destination"],
        token_file=credentials_dir / "agent-token",
        ca_file=profile.get("control_ca_file"),
        max_result_bytes=profile["limits"]["max_tool_result_bytes"],
    )
    try:
        bridge.start()
    except BaseException:
        bridge.close()
        raise
    return bridge


def catalog(profile: dict) -> int:
    """Print the offered tool catalog's pin; no model call, no prompt, no API key read."""
    if not allowed_tools(profile):
        print(json.dumps({"error": "no_tools_allowed"}))
        return 2
    bridge = build_tool_bridge(profile, _manager_credentials_dir())
    try:
        tools = bridge.tools()
        print(
            json.dumps(
                {
                    "tool_catalog_sha256": catalog_sha256(tools),
                    "tools": sorted(tool["name"] for tool in tools),
                }
            )
        )
    finally:
        bridge.close()
    return 0


def refresh_spend_rows(store, ledger, run_id: str) -> None:
    """Show the control plane's view of reservations still open locally, in the report.

    A person may have resolved an uncertain call's reservation on the control plane; the local
    row would otherwise keep reading ``reserved``. Only the report's rows change: the run's state
    and halt reason never do, and an unreachable control plane leaves every row as it was."""
    for row in store.spend_rows(run_id):
        if row["state"] != "reserved" or not row["call_id"]:
            continue
        seen = ledger.status(row["call_id"], run_id)
        if seen is not None and seen.status in {"settled", "released"}:
            store.spend_resolve(
                run_id, row["seq"], seen.status, seen.resolution, seen.settled_micro_cents
            )


def _refresh_report_spend(store, profile: dict, found, control=None) -> None:
    """Refresh before the receipt prints; billed destinations only, and never an error."""
    if not found.billed:
        return
    try:
        control = control or JsonEndpoint(
            profile["control_url"],
            os.environ[profile["agent_token_env"]],
            profile["destination"],
        )
        ledger = SpendLedger(
            control,
            (profile.get("model_spend") or {}).get("project_id"),
            profile["model"]["providerID"],
            f"{profile['model']['providerID']}/{profile['model']['id']}",
        )
        refresh_spend_rows(store, ledger, profile["run_id"])
    except Exception:  # noqa: BLE001 - the report prints whatever the journal holds
        return


class _Exit(Exception):
    """A closed exit: an exit code plus a sanitized reason code (never a value or secret)."""

    def __init__(self, code: int, reason: str):
        super().__init__(reason)
        self.code = code
        self.reason = reason if _REASON.fullmatch(reason) else "startup_refused"


def _log(message: str) -> None:
    print(f"tessera-reasoning: {message}", file=sys.stderr, flush=True)


def _closed_run(store, run_id: str) -> dict | None:
    """The journal row when ``run_id`` already ended (one-shot), else None."""
    try:
        row = store.get(run_id)
    except ValueError:  # Unknown run: a fresh request.
        return None
    return row if row["state"] in CLOSED_STATES else None


def _receipt(store, row: dict, run_id: str) -> str:
    receipt = {k: row[k] for k in ("id", "state", "reason", "tokens", "cost")}
    receipt["provenance"] = store.provenance_coverage(run_id)
    receipt["model_calls"] = store.model_calls(run_id, every=True)
    receipt["tool_calls"] = store.tool_calls(run_id)
    receipt["loop_parks"] = store.loop_parks(run_id)
    receipt["loop_resumes"] = store.loop_resumes(run_id)
    receipt["rate_limit"] = store.latest_rate_limit(run_id)
    # Reservations still open (an uncertain call is never replayed, and its worst case
    # keeps counting against the ledger's caps until an operator resolves it).
    receipt["spend_ledger"] = store.spend_rows(run_id)
    return json.dumps(receipt)


def main(argv=None):
    """Run one command; see the module docstring for the exit-code table."""
    try:
        return _main(argv)
    except _Exit as stop:
        kind = "refused" if stop.code == EXIT_REFUSED else "configuration error"
        _log(f"{kind} ({stop.reason})")
        return stop.code


def _main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "status", "stop", "report", "catalog"))
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--prompt-file", type=Path)
    parser.add_argument(
        "--run-id",
        help="Run identity for this request; defaults to the profile's run_id.",
    )
    args = parser.parse_args(argv)
    try:
        profile = json.loads(private_file(args.profile, credentials_dir=manager_credentials_dir()))
    except (OSError, ValueError):
        raise _Exit(EXIT_CONFIG, "profile_unreadable") from None
    if not isinstance(profile, dict):
        raise _Exit(EXIT_CONFIG, "profile_invalid")
    if args.run_id is not None:
        # A fresh request names its own run identity; the profile's stays the default.
        if not _ID.fullmatch(args.run_id):
            raise _Exit(EXIT_CONFIG, "run_id_invalid")
        profile["run_id"] = args.run_id
    if args.command == "catalog":
        # Obtaining the pin is the point: only a missing/placeholder pin is tolerated here.
        pin = profile.get("tool_catalog_sha256")
        checked = dict(profile)
        try:
            tools = allowed_tools(profile)
        except ValueError:
            tools = frozenset()  # validate_profile refuses the bad rules below.
        if tools and not (isinstance(pin, str) and _SHA256.fullmatch(pin)):
            checked["tool_catalog_sha256"] = _CATALOG_PLACEHOLDER
        validate_profile(checked)
        return catalog(profile)
    try:
        validate_profile(profile)
    except (KeyError, TypeError, ValueError):
        raise _Exit(EXIT_CONFIG, "profile_invalid") from None
    found = destination(profile["model"]["providerID"], profile["account_mode"])
    credentials_dir = None
    if args.command == "run" and found.runtime == RUNTIME_ANTHROPIC_MESSAGES:
        # The API key is read only from the manager-provided credential directory.
        refuse_api_key_environment(dict(os.environ))
        try:
            credentials_dir = _manager_credentials_dir()
        except RuntimeBlocked as error:
            raise _Exit(EXIT_CONFIG, str(error)) from None
    store = ReasoningStore(args.state_dir)
    lock = None
    bridge = None
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
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise _Exit(EXIT_REFUSED, "supervisor_already_running") from None
            closed = _closed_run(store, profile["run_id"])
            if closed is not None:
                # One-shot: a closed run identity is never resumed or rerun. No harness,
                # control-plane, ledger or model call; the journal's receipt is printed as is.
                print(_receipt(store, closed, profile["run_id"]), flush=True)
                _log(
                    f"run {profile['run_id']} already closed (state {closed['state']});"
                    " not rerun, no model call. Start a new run with a new run identity."
                )
                return EXIT_COMPLETED if closed["state"] == "completed" else EXIT_BLOCKED
            try:
                text = (
                    private_file(
                        args.prompt_file, credentials_dir=manager_credentials_dir()
                    ).decode()
                    if args.prompt_file
                    else None
                )
            except (OSError, ValueError):
                raise _Exit(EXIT_CONFIG, "prompt_unreadable") from None
            if text is not None and (not text.strip() or len(text.encode()) > 8192):
                raise _Exit(EXIT_CONFIG, "prompt_invalid")
            variable = profile.get("agent_token_env")
            if not isinstance(variable, str) or os.environ.get(variable) is None:
                raise _Exit(EXIT_CONFIG, "agent_token_missing")
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
            binding["profile_sha256"] = hashlib.sha256(
                private_file(args.profile, credentials_dir=manager_credentials_dir())
            ).hexdigest()
            try:
                store.bind(profile["run_id"], binding, time.time())
            except ValueError:
                # The same run identity with another profile/prompt: never rebound.
                raise _Exit(EXIT_REFUSED, "run_identity_rebind_refused") from None
            control = JsonEndpoint(
                profile["control_url"],
                os.environ[profile["agent_token_env"]],
                profile["destination"],
            )
            tools_blocked = None
            if credentials_dir is not None:
                options = {}
                if allowed_tools(profile):
                    # Tools are offered only when the profile allows them and the live
                    # catalog matches the operator's pin.
                    try:
                        bridge = build_tool_bridge(profile, credentials_dir)
                        if catalog_sha256(bridge.tools()) != profile["tool_catalog_sha256"]:
                            raise ToolBridgeBlocked("tool_catalog_changed")
                    except ToolBridgeBlocked as error:
                        code = str(error)
                        tools_blocked = code if _REASON.fullmatch(code) else "tool_bridge_blocked"
                    except Exception:  # noqa: BLE001 - fail closed with a code only
                        tools_blocked = "tool_bridge_unavailable"
                    options["tool_bridge"] = bridge
                harness = AnthropicMessagesReasoning(
                    profile, credentials_dir=credentials_dir, **options
                )
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
                # A billed destination always settles on the control plane's model-spend
                # ledger: there is no profile switch to run without it.
                spend=SpendLedger(
                    control,
                    (profile.get("model_spend") or {}).get("project_id"),
                    profile["model"]["providerID"],
                    f"{profile['model']['providerID']}/{profile['model']['id']}",
                )
                if found.billed
                else None,
            )
            for sig in (signal.SIGTERM, signal.SIGINT):
                # Honoured by the next fence, including between tool-loop turns.
                signal.signal(sig, lambda *_: store.stop(profile["run_id"]))
            if tools_blocked is not None:
                supervisor.halt(store.get(profile["run_id"]), tools_blocked)
            while tools_blocked is None:
                row = supervisor.tick(profile["run_id"], text)
                if row["state"] not in {"ready", "running"}:
                    break
                time.sleep(1)
            row = store.get(profile["run_id"])
            if profile.get("project_id"):
                with ProvenanceClient(
                    profile["control_url"],
                    os.environ[profile["agent_token_env"]],
                    profile["destination"],
                    timeout=1,
                ) as collector:
                    store.drain_provenance(profile["run_id"], collector, profile["project_id"])
        if args.command in {"run", "report"}:
            _refresh_report_spend(store, profile, found, control if args.command == "run" else None)
        printed = _receipt(store, row, profile["run_id"])
        print(printed)
        if args.command == "report":
            delivered = json.loads(printed)["provenance"]["delivery_complete"]
            return EXIT_COMPLETED if delivered else EXIT_BLOCKED
        if args.command == "run" and row["state"] != "completed":
            _log(f"run {profile['run_id']} ended {row['state']} ({row['reason'] or 'no reason'})")
        return EXIT_COMPLETED if row["state"] == "completed" else EXIT_BLOCKED
    finally:
        if bridge is not None:
            bridge.close()
        store.close()
        if lock is not None:
            os.close(lock)


if __name__ == "__main__":
    raise SystemExit(main())
