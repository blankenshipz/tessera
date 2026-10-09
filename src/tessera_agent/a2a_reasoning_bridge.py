"""Optional trusted A2A critic supervisor; exact operator-approved prompt only.

No inferred profile, prompt rewriting, provider setup or provenance delivery.
Run independently of the reference-only peer with a private task-specific mapping.
"""

import argparse
import asyncio
import fcntl
import hashlib
import json
import os
import re
import signal
import time
from pathlib import Path

from tessera_control.a2a_store import A2AStore, PeerConflict

from .remote_reasoning_runtime import (
    JsonEndpoint,
    OpenCodeV2Reasoning,
    ReasoningSupervisor,
    RuntimeBlocked,
    validate_profile,
)
from .remote_reasoning_store import ReasoningStore, private_file

ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,199}\Z")
DIGEST = re.compile(r"[a-f0-9]{64}\Z")


class GuardedHarness:
    def __init__(self, bridge, harness):
        self.bridge, self.harness = bridge, harness

    def verify(self):
        self.bridge.guard()
        return self.harness.verify()

    def create(self, row):
        self.bridge.guard()
        with self.bridge.effect_gate():
            return self.harness.create(row)

    def prompt(self, row, text):
        self.bridge.guard()
        if hashlib.sha256(text.encode()).hexdigest() != self.bridge.profile["prompt_sha256"]:
            raise RuntimeBlocked("bridge_prompt_changed")
        with self.bridge.effect_gate():
            return self.harness.prompt(row, text)

    def status(self, row):
        self.bridge.guard()
        return self.harness.status(row)

    def interrupt(self, row):
        # Interrupt is cleanup, allowed after cancellation/expiry/revocation.
        return self.harness.interrupt(row)


class A2AReasoningBridge:
    def __init__(
        self,
        peer,
        runtime,
        peer_config,
        mapping,
        profile_bytes,
        prompt_bytes,
        control,
        harness,
        exchange_verifier,
        clock=time.time,
    ):
        if set(mapping) != {
            "task_id",
            "tenant",
            "sender_id",
            "project_id",
            "context_id",
            "profile_file",
            "profile_sha256",
            "prompt_file",
        }:
            raise ValueError("Closed operator task mapping required")
        for key in ("task_id", "tenant", "sender_id", "project_id", "context_id"):
            if not isinstance(mapping[key], str) or not ID.fullmatch(mapping[key]):
                raise ValueError("Invalid operator mapping identifier")
        if (
            not DIGEST.fullmatch(mapping["profile_sha256"])
            or hashlib.sha256(profile_bytes).hexdigest() != mapping["profile_sha256"]
        ):
            raise ValueError("Approved profile digest changed")
        profile = json.loads(profile_bytes)
        validate_profile(profile)
        if any(type(profile[key]) is not int or profile[key] < 1 for key in ("revision", "epoch")):
            raise ValueError("Reasoning profile requires exact positive grant pins")
        if (
            not prompt_bytes.strip()
            or len(prompt_bytes) > 8192
            or hashlib.sha256(prompt_bytes).hexdigest() != profile["prompt_sha256"]
        ):
            raise ValueError("Approved prompt digest changed")
        if (
            profile["subject_credential_id"] != peer_config["credential_id"]
            or profile["destination"] != peer_config["destination"]
            or profile["model"]["id"] != peer_config["model"]
            or profile["control_url"] != peer_config["control_url"]
        ):
            raise ValueError("Reasoning profile differs from enrolled recipient")
        self.peer, self.runtime, self.config, self.mapping = peer, runtime, peer_config, mapping
        self.profile, self.prompt, self.control = profile, prompt_bytes.decode("utf-8"), control
        self.exchange_verifier, self.clock = exchange_verifier, clock
        self.shutdown_requested = False
        self.last_denial = False
        self.identity = {
            "tenant": mapping["tenant"],
            "agent_id": mapping["sender_id"],
            "project_id": mapping["project_id"],
        }
        self.task = self.get_task()
        if self.task["metadata"]["reference"] != mapping["context_id"]:
            raise ValueError("Task does not name approved curated context")
        self.fence = self.task["metadata"]["authority"]
        self.peer_message_id = self.task["history"][0]["messageId"]
        if not ID.fullmatch(self.peer_message_id):
            raise ValueError("Invalid peer message reference")
        self.binding = {
            "run_id": profile["run_id"],
            "profile_sha256": mapping["profile_sha256"],
            "context_id": mapping["context_id"],
            "prompt_sha256": profile["prompt_sha256"],
            "reasoning_grant_id": profile["grant_id"],
            "exchange_authority": self.fence,
        }
        self.supervisor = ReasoningSupervisor(
            runtime, GuardedHarness(self, harness), self.reasoning_authority, profile, clock
        )

    def get_task(self):
        return self.peer.get(
            self.mapping["tenant"], self.mapping["sender_id"], self.mapping["task_id"]
        )

    def reasoning_authority(self, grant_id):
        if grant_id != self.profile["grant_id"]:
            raise RuntimeBlocked("bridge_grant_changed")
        grant = self.control.call("GET", "/agent-authority-grants/" + grant_id)
        if (
            grant.get("org_id") != self.mapping["tenant"]
            or grant.get("project_id") != self.mapping["project_id"]
            or self.fence["binding"] not in grant.get("bounds", {}).get("bindings", [])
        ):
            self.last_denial = True
            raise RuntimeBlocked("bridge_reasoning_scope_changed")
        return grant

    def effect_gate(self):
        return self.peer.reasoning_effect_gate(
            self.mapping["tenant"], self.mapping["sender_id"], self.mapping["task_id"]
        )

    def guard(self):
        if self.shutdown_requested:
            raise RuntimeBlocked("bridge_supervisor_stopped")
        task = self.get_task()
        if task["status"]["state"] == "TASK_STATE_CANCELED":
            raise RuntimeBlocked("bridge_peer_canceled")
        if task["metadata"]["authority"] != self.fence:
            self.last_denial = True
            raise RuntimeBlocked("bridge_exchange_changed")
        try:
            asyncio.run(self.exchange_verifier(self.identity, self.fence))
            context = self.control.call("GET", "/agent-contexts/" + self.mapping["context_id"])
            text = context.get("body")
            if (
                context.get("id") != self.mapping["context_id"]
                # The existing context wire record omits org_id; control GET
                # authorizes tenant/project, and both grant reads pin the tenant.
                or context.get("org_id") not in {None, self.mapping["tenant"]}
                or context.get("project_id") != self.mapping["project_id"]
                or context.get("credential_id") != self.profile["subject_credential_id"]
                or context.get("destination") != self.profile["destination"]
                or context.get("disclosure_approved") is not True
                or not isinstance(text, str)
                or hashlib.sha256(text.encode()).hexdigest() != self.profile["prompt_sha256"]
            ):
                raise RuntimeBlocked("bridge_context_not_approved_or_changed")
        except RuntimeBlocked:
            self.last_denial = True
            raise
        except Exception:
            self.last_denial = True
            raise RuntimeBlocked("bridge_exchange_or_context_denied") from None

    def record(self, state, row=None):
        outcome = {
            "claimed": "requested",
            "exchange": "requested",
            "running": "started",
            "completed": "succeeded",
            "failed": "failed",
            "denied": "denied",
            "canceled": "canceled",
            "stop_acknowledged": "unknown",
            "stop_unknown": "unknown",
        }.get(state, "unknown")
        digest = hashlib.sha256((self.mapping["task_id"] + ":" + state).encode()).hexdigest()
        references = {
            "task_id": self.fence["binding"]["task_id"],
            "grant_id": self.fence["grant_id"] if state == "exchange" else self.profile["grant_id"],
            "context_id": self.mapping["context_id"],
        }
        remote = {
            "task_id": self.mapping["task_id"],
            "peer_id": self.mapping["sender_id"],
            "message_id": row["message_id"] if row else self.peer_message_id,
        }
        proof = {
            "grant_id": references["grant_id"],
            "expected_revision": int(self.fence["revision"])
            if state == "exchange"
            else self.profile["revision"],
            "expected_epoch": int(self.fence["epoch"])
            if state == "exchange"
            else self.profile["epoch"],
        }
        record = {
            "record_id": "a2a_provenance_" + digest,
            "kind": "delegation"
            if state == "exchange"
            else "agent"
            if state == "session"
            else "permission"
            if state in {"denied", "canceled"}
            else "model",
            "outcome": outcome,
            "correlation_id": row["session_id"] if state == "session" else self.profile["run_id"],
            "causation_id": self.profile["run_id"]
            if state == "session"
            else row["session_id"]
            if row
            else self.mapping["task_id"],
            "model_id": self.profile["model"]["id"],
            "references": references,
            "remote": remote,
            "content_digest": "sha256:" + self.profile["prompt_sha256"],
            "error_code": "permission_denied"
            if state == "denied"
            else "canceled"
            if state == "canceled"
            else "outcome_unknown"
            if outcome == "unknown"
            else None,
        }
        return {
            "record": record,
            "grant_proof": proof,
            "links": {
                "peer_task_id": self.mapping["task_id"],
                "peer_message_id": self.peer_message_id,
                "run_id": self.profile["run_id"],
                "session_id": row["session_id"] if row else None,
                "message_id": row["message_id"] if row else None,
                "context_id": self.mapping["context_id"],
                "profile_sha256": self.mapping["profile_sha256"],
            },
        }

    def tick(self):
        link = self.peer.reasoning_link(
            self.mapping["tenant"], self.mapping["sender_id"], self.mapping["task_id"]
        )
        if link and link["binding"] != json.dumps(
            self.binding, sort_keys=True, separators=(",", ":")
        ):
            raise PeerConflict("Reasoning profile/task cannot be rebound")
        if not link:
            self.peer.link_reasoning(
                self.mapping["tenant"],
                self.mapping["sender_id"],
                self.mapping["task_id"],
                self.binding,
                self.record("claimed"),
                self.record("exchange"),
            )
        binding = {
            key: self.profile[key]
            for key in (
                "grant_id",
                "revision",
                "epoch",
                "subject_credential_id",
                "destination",
                "prompt_sha256",
            )
        }
        binding.update(
            model=self.profile["model"]["id"], profile_sha256=self.mapping["profile_sha256"]
        )
        row = self.runtime.bind(self.profile["run_id"], binding, self.clock())
        if self.shutdown_requested or self.get_task()["status"]["state"] == "TASK_STATE_CANCELED":
            self.runtime.stop(row["id"])
            self.supervisor.halt(
                row,
                "bridge_supervisor_stopped" if self.shutdown_requested else "bridge_peer_canceled",
            )
            row = self.runtime.get(row["id"])
        else:
            row = self.supervisor.tick(row["id"], self.prompt)
        denied_reasons = {
            "grant_expired",
            "grant_revision_changed",
            "grant_revoked",
            "grant_scope_changed",
            "budget_exhausted",
            "duration_outside_grant",
            "duration_expired",
            "usage_fence",
            "bridge_context_not_approved_or_changed",
            "bridge_exchange_or_context_denied",
            "bridge_reasoning_scope_changed",
            "bridge_exchange_changed",
        }
        denial = None
        if self.get_task()["status"]["state"] == "TASK_STATE_CANCELED":
            denial = self.record("canceled", row)
        elif self.last_denial or row.get("reason") in denied_reasons:
            denial = self.record("denied", row)
        self.peer.reasoning_observation(
            self.mapping["tenant"],
            self.mapping["sender_id"],
            self.mapping["task_id"],
            row,
            self.record(row["state"], row),
            denial,
            self.record("session", row),
        )
        return {
            key: row[key] for key in ("id", "state", "session_id", "message_id", "tokens", "cost")
        }


def drain_outbox(peer, mapping, client, *, limit=100):
    """One bounded delivery pass; uncertainty/denial retain immutable pending history."""
    from .provenance_client import DeliveryResult, stable_key

    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Outbox drain requires a 1..100 record bound")
    scope = (mapping["tenant"], mapping["sender_id"], mapping["task_id"])
    for item in peer.provenance_outbox(*scope, pending_only=True, limit=limit):
        envelope = item["envelope"]
        payload = {key: envelope[key] for key in ("record", "grant_proof")}
        record_id = payload["record"]["record_id"]
        peer.provenance_delivery(
            *scope, payload, DeliveryResult("unknown", "delivery_started", record_id), started=True
        )
        try:
            result = client.append(
                mapping["project_id"],
                payload["record"],
                payload["grant_proof"],
                idempotency_key=stable_key(record_id),
            )
        except ValueError:
            result = DeliveryResult("rejected", "invalid_envelope", record_id)
        except Exception:
            result = DeliveryResult("unknown", "transport_failure", record_id)
        peer.provenance_delivery(*scope, payload, result)
    return peer.provenance_summary(*scope)


def delivery_client(mapping, config, profile_bytes):
    from .provenance_client import ProvenanceClient

    if hashlib.sha256(profile_bytes).hexdigest() != mapping["profile_sha256"]:
        raise ValueError("Approved profile digest changed")
    profile = json.loads(profile_bytes)
    validate_profile(profile)
    if (
        profile["subject_credential_id"] != config["credential_id"]
        or profile["destination"] != config["destination"]
        or profile["control_url"] != config["control_url"]
    ):
        raise ValueError("Delivery profile differs from enrolled recipient")
    return ProvenanceClient(
        profile["control_url"], os.environ[profile["agent_token_env"]], profile["destination"]
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", default="run", choices=("run", "drain-outbox"))
    parser.add_argument("--mapping", type=Path, required=True)
    parser.add_argument("--peer-config", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--deliver-provenance", action="store_true")
    args = parser.parse_args(argv)
    mapping = json.loads(private_file(args.mapping))
    config = json.loads(private_file(args.peer_config))
    profile_bytes = private_file(Path(mapping["profile_file"]))
    peer = A2AStore(Path(config["database"]))
    if args.command == "drain-outbox":
        with delivery_client(mapping, config, profile_bytes) as client:
            result = drain_outbox(peer, mapping, client, limit=args.limit)
        print(json.dumps(result))
        return 0 if result["outbox_fully_acknowledged"] else 2
    if args.state_dir is None:
        parser.error("run requires --state-dir")
    profile = json.loads(profile_bytes)
    prompt_bytes = private_file(Path(mapping["prompt_file"]))
    control = JsonEndpoint(
        profile["control_url"], os.environ[profile["agent_token_env"]], profile["destination"]
    )
    harness = OpenCodeV2Reasoning(
        JsonEndpoint(
            profile["harness_url"],
            os.environ[profile["harness_token_env"]],
            auth_mode=profile.get("harness_auth_mode", "basic"),
        ),
        profile,
    )
    from tessera_control.a2a_peer import ControlAuthorityVerifier

    runtime = ReasoningStore(args.state_dir)
    lock = os.open(
        args.state_dir / "supervisor.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
    )
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        bridge = A2AReasoningBridge(
            peer,
            runtime,
            config,
            mapping,
            profile_bytes,
            prompt_bytes,
            control,
            harness,
            ControlAuthorityVerifier(config),
        )
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: setattr(bridge, "shutdown_requested", True))
        while True:
            result = bridge.tick()
            if result["state"] != "running":
                break
            time.sleep(1)
        if args.deliver_provenance:
            with delivery_client(mapping, config, profile_bytes) as client:
                result["provenance"] = drain_outbox(peer, mapping, client, limit=args.limit)
        print(json.dumps(result))
        return (
            0
            if result["state"] == "completed"
            and result.get("provenance", {}).get("outbox_fully_acknowledged", True)
            else 2
        )
    finally:
        os.close(lock)
        runtime.close()


if __name__ == "__main__":
    raise SystemExit(main())
