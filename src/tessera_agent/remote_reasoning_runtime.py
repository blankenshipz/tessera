"""Standalone supervised OpenCode V2 reasoning, separate from the coordinator.

Only an operator-pinned dedicated harness is supported. No provider credentials,
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
from urllib.request import Request

from tessera_worker.opencode_http import OpenCodeHttpClient, _default_open

from .provenance_client import ProvenanceClient
from .remote_reasoning_store import ReasoningStore, private_file

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class RuntimeBlocked(Exception):
    """Sanitized fail-closed failure; never includes upstream bodies."""


class JsonEndpoint:
    def __init__(
        self, origin: str, token: str, destination: str | None = None, *, auth_mode="bearer"
    ):
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
        return {"tokens": int(tokens), "cost": cost, "outcome": data.get("outcome")}

    def interrupt(self, row: dict):
        # Cleanup is allowed after authority expiry; never resumes parked input.
        self.endpoint.call("POST", f"/api/session/{row['session_id']}/interrupt?resume=false")


class ReasoningSupervisor:
    def __init__(self, store, harness, authority, profile, clock=time.time, public_capture=None):
        self.store, self.harness, self.authority = store, harness, authority
        self.profile, self.clock = profile, clock
        self.public_capture = public_capture

    def fence(self, row: dict):
        binding = json.loads(row["binding"])
        if row["stop_requested"]:
            raise RuntimeBlocked("stop_requested")
        if self.clock() >= row["started"] + self.profile["max_duration_seconds"]:
            raise RuntimeBlocked("duration_expired")
        if row["tokens"] >= self.profile["max_tokens"] or row["cost"] > 0:
            raise RuntimeBlocked("usage_fence")
        grant = self.authority(binding["grant_id"])
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
        if self.profile["max_duration_seconds"] > bounds["max_duration_seconds"]:
            raise RuntimeBlocked("duration_outside_grant")
        self.store.permission_current(row["id"])

    def effect(self, row, boundary, callback):
        previous = self.store.effect(row["id"], boundary)
        if previous == "acknowledged":
            return
        if not self.store.begin(row["id"], boundary):
            raise RuntimeBlocked("uncertain_" + boundary)
        try:
            callback()
        except Exception:
            self.store.finish(row["id"], boundary, "uncertain")
            raise RuntimeBlocked("uncertain_" + boundary) from None
        self.store.finish(row["id"], boundary, "acknowledged")

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
            self.harness.verify()
            self.fence(self.store.get(run_id))
            self.effect(row, "create", lambda: self.harness.create(row))
            self.fence(self.store.get(run_id))
            if self.store.effect(run_id, "prompt") != "acknowledged":
                if text is None or hashlib.sha256(
                    text.encode()
                ).hexdigest() != self.store.input_digest(run_id):
                    raise RuntimeBlocked("prompt_required_or_changed")
                self.effect(row, "prompt", lambda: self.harness.prompt(row, text))
            observation = self.harness.status(row)
            self.store.usage(run_id, observation["tokens"], observation["cost"])
            self.fence(self.store.get(run_id))
            if self.public_capture is not None:
                # Only a separately admitted normalized producer may supply events.
                # Session usage, prompt text and private parts are not capture input.
                self.public_capture.after_status(self.store.get(run_id), observation)
                self.fence(self.store.get(run_id))
            outcome = observation["outcome"]
            self.store.state(
                run_id,
                "completed" if outcome == "succeeded" else "failed" if outcome else "running",
            )
        except Exception as error:
            reason = (
                str(error) if isinstance(error, RuntimeBlocked) else "invalid_authority_or_usage"
            )
            self.halt(row, reason)
        return self.store.get(run_id)


def validate_profile(profile: dict, *, skill_id=None):
    for field in ("run_id", "agent", "grant_id", "subject_credential_id"):
        if not _ID.fullmatch(profile[field]):
            raise ValueError("Invalid runtime identifier.")
    if (
        profile["account_mode"] != "included-chatgpt-oauth"
        or profile["model"]["providerID"] != "openai"
    ):
        raise ValueError("Only an operator-attested included ChatGPT OAuth harness is supported.")
    for field, maximum in (
        ("max_steps", 16),
        ("max_tokens", 32768),
        ("max_duration_seconds", 3600),
    ):
        if type(profile[field]) is not int or not 0 < profile[field] <= maximum:
            raise ValueError("Invalid runtime usage bound.")
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
            harness = OpenCodeV2Reasoning(
                JsonEndpoint(
                    profile["harness_url"],
                    os.environ[profile["harness_token_env"]],
                    auth_mode=profile.get("harness_auth_mode", "basic"),
                ),
                profile,
            )
            supervisor = ReasoningSupervisor(
                store,
                harness,
                lambda gid: control.call("GET", "/agent-authority-grants/" + gid),
                profile,
            )
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda *_: store.stop(profile["run_id"]))
            while True:
                row = supervisor.tick(profile["run_id"], text)
                if row["state"] != "running":
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
