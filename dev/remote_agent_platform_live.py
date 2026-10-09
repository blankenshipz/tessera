"""Opt-in installed-service journey. Never starts/deploys services or invokes inference.

The private manifest supplies exact approved requests/assertions, not executable hooks.
Only an operator may run this after component convergence and ownership inspection.
"""

import argparse
import asyncio
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import sys
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

STAGES = (
    "planner_proposal",
    "critic_discovery",
    "critic_task",
    "authority_request",
    "human_issue",
    "agent_admit",
    "agent_dispatch",
    "retained_output",
    "denied_expansion",
    "lost_reply_replay",
    "restart_baseline",
    "restart_observed",
    "expiry_fence",
    "human_revoke",
    "revocation_fence",
    "joined_export",
)
TOOLS = {
    "tessera_propose_research",
    "tessera_request_authority",
    "tessera_consume_grant",
    "tessera_authority_status",
    "tessera_notebook",
    "tessera_export_notebook",
}
MODES = {
    "planner_proposal": "mcp",
    "critic_discovery": "a2a",
    "critic_task": "a2a",
    "authority_request": "mcp",
    "human_issue": "human",
    "agent_admit": "mcp",
    "agent_dispatch": "mcp",
    "retained_output": "human",
    "denied_expansion": "mcp",
    "lost_reply_replay": "mcp",
    "restart_baseline": "human",
    "restart_observed": "human",
    "expiry_fence": "mcp",
    "human_revoke": "human",
    "revocation_fence": "mcp",
    "joined_export": "mcp",
}


class Blocked(Exception):
    pass


class Unknown(Exception):
    pass


def private_json(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_size > 1024 * 1024
        ):
            raise Blocked("private_manifest_required")
        return json.load(stream)


def origin(value):
    u = urlsplit(value)
    if (
        u.scheme not in {"http", "https"}
        or not u.hostname
        or u.username
        or u.password
        or u.query
        or u.fragment
        or u.path not in {"", "/"}
    ):
        raise Blocked("invalid_origin")
    if u.scheme == "http" and u.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise Blocked("remote_requires_tls")
    return value.rstrip("/")


def select(value, path):
    for part in path.split("."):
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


def resolve(value, observed):
    if isinstance(value, dict):
        if set(value) == {"$ref"}:
            stage, path = value["$ref"].split(".", 1)
            return select(observed[stage], path)
        return {k: resolve(v, observed) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, observed) for v in value]
    return value


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def receipt(manifest, results):
    # Closed projection: no URLs, filenames, messages, payloads, exception text or IDs.
    return {
        "schema": 1,
        "mode": "live",
        "environment": manifest["environment"],
        "independent_host_verified": False,
        "laptop_off_verified": False,
        "reasoning_verified": False,
        "integrated_acceptance_verified": False,
        "status": "passed"
        if len(results) == len(STAGES) and all(r["status"] == "passed" for r in results)
        else "incomplete",
        "checks": [{k: r[k] for k in ("stage", "status", "code")} for r in results],
        "limitations": [
            "self_reported_assertions",
            "client_reply_discard_not_network_fault",
            "external_restart_required",
            "no_scientific_validity_claim",
        ],
    }


def no_inline_secrets(value):
    if isinstance(value, dict):
        for k, v in value.items():
            if k.lower() in {"token", "password", "authorization", "api_key", "secret"}:
                raise Blocked("inline_secret_forbidden")
            no_inline_secrets(v)
    elif isinstance(value, list):
        for v in value:
            no_inline_secrets(v)


def validate(m):
    no_inline_secrets(m)
    if m.get("peer_paid_inference_disabled") is not True:
        raise Blocked("no_paid_peer_inference_required")
    if m.get("environment") not in {"local_owned_docker", "authorized_remote"}:
        raise Blocked("environment_required")
    if m.get("ownership_inspected") is not True or m.get("writers_quiesced") is not True:
        raise Blocked("convergence_and_ownership_required")
    if m.get("max_seconds", 0) not in range(1, 601):
        raise Blocked("bounded_deadline_required")
    origin(m["control_url"])
    origin(m["critic_url"])
    steps = m["steps"]
    if tuple(s["stage"] for s in steps) != STAGES:
        raise Blocked("exact_journey_required")
    for s in steps:
        expected_modes = {MODES[s["stage"]]}
        if s["stage"] in {"restart_baseline", "restart_observed"}:
            expected_modes.add("kubernetes")
        if s["via"] not in expected_modes or not s.get("assertions"):
            raise Blocked("stage_contract_required")
        if s["via"] == "kubernetes":
            probe = m["restart_probe"]
            if not all(
                isinstance(probe.get(k), str) and probe[k]
                for k in (
                    "kubeconfig",
                    "context",
                    "namespace",
                    "namespace_uid",
                    "deployment",
                    "deployment_uid",
                )
            ):
                raise Blocked("explicit_restart_resource_pins_required")
            for k in ("namespace", "deployment", "context"):
                if not re.fullmatch(r"[A-Za-z0-9_.-]+", probe[k]):
                    raise Blocked("invalid_restart_pin")
        if s["via"] == "mcp" and s.get("tool") not in TOOLS:
            raise Blocked("unsupported_tool")
        required_tool = {
            "planner_proposal": "tessera_propose_research",
            "authority_request": "tessera_request_authority",
            "joined_export": "tessera_export_notebook",
            "agent_admit": "tessera_consume_grant",
            "agent_dispatch": "tessera_consume_grant",
            "denied_expansion": "tessera_consume_grant",
            "lost_reply_replay": "tessera_consume_grant",
            "expiry_fence": "tessera_consume_grant",
            "revocation_fence": "tessera_consume_grant",
        }.get(s["stage"])
        if required_tool and s.get("tool") != required_tool:
            raise Blocked("stage_tool_mismatch")
        if s["stage"] in {"denied_expansion", "expiry_fence", "revocation_fence"}:
            if not any(
                c["path"] == "http_status"
                and c.get("op", "eq") == "eq"
                and c["value"] in {403, 409, 410}
                for c in s["assertions"]
            ):
                raise Blocked("explicit_fence_status_required")
        if s.get("poll_seconds", 0) not in range(0, 61):
            raise Blocked("bounded_poll_required")
        if s.get("poll_seconds") and s["stage"] not in {"retained_output", "restart_observed"}:
            raise Blocked("only_safe_read_polling")
        if s["stage"] == "restart_observed" and not any(
            c.get("op") == "ne"
            and isinstance(c["value"], dict)
            and str(c["value"].get("$ref", "")).startswith("restart_baseline.")
            for c in s["assertions"]
        ):
            raise Blocked("restart_generation_change_required")
        if s["stage"] == "expiry_fence":
            expiry = datetime.fromisoformat(s["wait_until"])
            if expiry.tzinfo is None:
                raise Blocked("aware_expiry_required")
        if s["via"] == "human":
            path = s.get("path", "")
            if not path.startswith("/") or "//" in path or ".." in path or "?" in path:
                raise Blocked("invalid_human_path")
            if s.get("method") not in {"GET", "POST"}:
                raise Blocked("invalid_method")
            if s["method"] == "POST" and not s.get("idempotency_key"):
                raise Blocked("idempotency_key_required")
            if s["stage"] in {"retained_output", "restart_baseline", "restart_observed"}:
                if s["method"] != "GET":
                    raise Blocked("read_only_stage_required")
            if s["stage"] == "human_issue" and (
                s["method"] != "POST"
                or path != f"/projects/{m['project_id']}/agent-authority-grants"
            ):
                raise Blocked("exact_issue_route_required")
            if s["stage"] == "human_revoke" and (
                s["method"] != "POST"
                or not re.fullmatch(
                    r"/agent-authority-grants/(?:[A-Za-z0-9_-]+|\{grant_id\})/revoke", path
                )
            ):
                raise Blocked("exact_revoke_route_required")
        if s["via"] == "a2a" and not m.get("critic_agent_id"):
            raise Blocked("exact_critic_identity_required")
        if s["via"] == "a2a" and s["stage"] == "critic_task":
            if not isinstance(s.get("request", {}).get("message"), dict):
                raise Blocked("a2a_v1_send_message_request_required")


def assertions(step, value, observed):
    for check in step["assertions"]:
        actual = select(value, check["path"])
        expected = resolve(check["value"], observed)
        op = check.get("op", "eq")
        if op not in {"eq", "ne", "contains", "nonempty"}:
            raise Blocked("unsupported_assertion")
        if op == "eq":
            ok = actual == expected
        elif op == "ne":
            ok = actual != expected
        elif op == "contains":
            ok = expected in actual
        else:
            ok = bool(actual)
        if not ok:
            raise AssertionError("assertion_failed")


def kubernetes_generation(probe):
    """Read actual container starts with namespace/Deployment/ReplicaSet ownership fences."""
    base = ["kubectl", "--kubeconfig", probe["kubeconfig"], "--context", probe["context"]]

    def get(*args):
        result = subprocess.run(
            base + list(args) + ["-o", "json"], capture_output=True, timeout=10, check=False
        )
        if result.returncode or len(result.stdout) > 2 * 1024 * 1024:
            raise Unknown("kubernetes_probe_unavailable")
        return json.loads(result.stdout)

    namespace = get("get", "namespace", probe["namespace"])
    if namespace["metadata"]["uid"] != probe["namespace_uid"]:
        raise Blocked("namespace_pin_changed")
    ns = ["-n", probe["namespace"]]
    deployment = get(*ns, "get", "deployment", probe["deployment"])
    if deployment["metadata"]["uid"] != probe["deployment_uid"]:
        raise Blocked("deployment_pin_changed")
    labels = deployment["spec"]["selector"].get("matchLabels", {})
    if not labels or deployment["spec"]["selector"].get("matchExpressions"):
        raise Blocked("unsupported_deployment_selector")
    selector = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
    replicas = get(*ns, "get", "replicasets", "-l", selector)["items"]
    owned = {
        r["metadata"]["uid"]
        for r in replicas
        if any(
            x.get("controller") and x.get("uid") == probe["deployment_uid"]
            for x in r["metadata"].get("ownerReferences", [])
        )
    }
    pods = get(*ns, "get", "pods", "-l", selector)["items"]
    starts = []
    for pod in pods:
        if not any(
            x.get("controller") and x.get("uid") in owned
            for x in pod["metadata"].get("ownerReferences", [])
        ):
            raise Blocked("pod_owner_mismatch")
        if pod["metadata"].get("deletionTimestamp"):
            continue
        containers = pod.get("status", {}).get("containerStatuses", [])
        if not containers or not all(
            c.get("ready") and c.get("state", {}).get("running") for c in containers
        ):
            raise Unknown("deployment_process_not_ready")
        starts.append(
            {
                "pod_uid": pod["metadata"]["uid"],
                "containers": [
                    {
                        "name": c["name"],
                        "restart_count": c["restartCount"],
                        "started_at": c["state"]["running"]["startedAt"],
                    }
                    for c in sorted(containers, key=lambda c: c["name"])
                ],
            }
        )
    if not starts:
        raise Unknown("deployment_process_absent")
    return {
        "body": {
            "generation": digest(sorted(starts, key=lambda s: s["pod_uid"])),
            "deployment_uid": probe["deployment_uid"],
            "ready": True,
        }
    }


def typed_arguments(step, observed):
    """Validate against implemented SDK wire models after resolving actual response pins."""
    from tessera_mcp.mcp_workflow import AuthorityBounds, Consume, ResearchProposal

    args = resolve(step.get("arguments", {}), observed)
    field, schema = {
        "tessera_request_authority": ("bounds", AuthorityBounds),
        "tessera_consume_grant": ("payload", Consume),
        "tessera_propose_research": ("payload", ResearchProposal),
    }.get(step["tool"], (None, None))
    if schema:
        args[field] = schema.model_validate(args[field]).model_dump(mode="json")
    action = {"agent_admit": "attempt:admit", "agent_dispatch": "attempt:dispatch"}.get(
        step["stage"]
    )
    if action and args["payload"]["command"]["action"] != action:
        raise Blocked("stage_command_mismatch")
    return args


def validate_critic_card(card, manifest):
    if card.name != manifest["critic_agent_id"] or len(card.supported_interfaces) != 1:
        raise Blocked("unexpected_critic_card")
    interface = card.supported_interfaces[0]
    if (
        interface.protocol_version != "1.0"
        or interface.protocol_binding != "JSONRPC"
        or interface.url.rstrip("/") != manifest["critic_url"]
    ):
        raise Blocked("unexpected_critic_destination_or_protocol")


def private_write(path, value, *, replace=False):
    """Private durable setup state; never emits credentials or overwrites another run."""
    path = Path(path)
    if path.is_symlink():
        raise Blocked("setup_symlink_forbidden")
    temp = path.with_name(path.name + "." + secrets.token_hex(8)) if replace else path
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(value if isinstance(value, bytes) else json.dumps(value).encode())
        stream.flush()
        os.fsync(stream.fileno())
    if replace:
        os.replace(temp, path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


async def setup_local(manifest, client):
    """Bounded ordinary human enrollment; agents retain their own separate identities.

    State is intent-before-effect. Unknown setup calls need explicit same-key operator
    reconciliation; they are never automatically replayed or replaced.
    """
    from tessera_control.models import CreateCredential, ExternalAgentScope
    from tessera_mcp.config import Config

    no_inline_secrets(manifest)
    config = manifest["local_setup"]
    if (
        manifest["environment"] != "local_owned_docker"
        or manifest.get("writers_quiesced") is not True
        or manifest.get("ownership_inspected") is not True
        or config.get("human_approved") is not True
    ):
        raise Blocked("approved_quiesced_local_setup_required")
    root = Path(config["private_dir"])
    if not root.is_absolute():
        raise Blocked("absolute_setup_directory_required")
    root.mkdir(mode=0o700, exist_ok=True)
    info = root.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise Blocked("private_setup_directory_required")
    parent_fd = os.open(root.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)
    if config.get("duration_seconds", 0) not in range(60, 7201):
        raise Blocked("bounded_setup_lifetime_required")
    roles = config["agents"]
    if set(roles) != {"planner", "critic"}:
        raise Blocked("exact_local_roles_required")
    for role in roles.values():
        ExternalAgentScope.model_validate(role)
    state_path = root / "setup-state.json"
    binding = digest(
        {
            "origin": origin(manifest["control_url"]),
            "project_id": manifest["project_id"],
            "config": config,
        }
    )
    if state_path.exists():
        state = private_json(state_path)
        if state["binding"] != binding:
            raise Blocked("setup_intent_changed")
    else:
        if any(root.iterdir()):
            raise Blocked("new_setup_directory_must_be_empty")
        state = {
            "binding": binding,
            "expires_at": (
                datetime.now(UTC) + timedelta(seconds=config["duration_seconds"])
            ).isoformat(),
            "commands": {},
        }
        private_write(state_path, state)
    if datetime.fromisoformat(state["expires_at"]) <= datetime.now(UTC):
        raise Blocked("setup_identity_expired_reconcile_before_replacement")

    def token(path):
        return Config(
            manifest["control_url"], Path(path), manifest["project_id"], "local-human-setup"
        ).token()

    async def command(name, path, payload, token_file, destination=None):
        request_digest = digest(payload)
        entry = state["commands"].get(name)
        if entry:
            if entry["request_digest"] != request_digest or entry["path"] != path:
                raise Blocked("setup_command_changed")
            if entry["status"] != "confirmed":
                raise Unknown("setup_reply_unknown_reconcile_exact_intent")
            return entry["response"]
        entry = {
            "path": path,
            "request_digest": request_digest,
            "key": "local-setup-" + secrets.token_hex(16),
            "status": "pending",
        }
        state["commands"][name] = entry
        private_write(state_path, state, replace=True)
        headers = {"Authorization": "Bearer " + token(token_file), "Idempotency-Key": entry["key"]}
        if destination:
            headers["X-Agent-Destination"] = destination
        try:
            response = await client.post(
                manifest["control_url"] + path, headers=headers, json=payload
            )
            if response.status_code not in {200, 201} or len(response.content) > 2 * 1024 * 1024:
                raise Unknown("setup_rejected_or_unknown")
            value = response.json()
            # Credential API has a closed non-secret response; other setup records are
            # intentionally stored only as opaque IDs/revisions, never arbitrary body text.
            safe = {
                k: value[k]
                for k in (
                    "credential_id",
                    "principal_id",
                    "org_id",
                    "kind",
                    "audience",
                    "project_id",
                    "expires_at",
                    "id",
                    "revision",
                    "epoch",
                    "worker_principal_id",
                )
                if k in value
            }
            entry.update(status="confirmed", response=safe)
            private_write(state_path, state, replace=True)
            return safe
        except Exception:
            entry["status"] = "unknown"
            private_write(state_path, state, replace=True)
            raise Unknown("setup_outcome_unknown_no_retry") from None

    result = {}
    for name in ("planner", "critic"):
        path = root / (name + "-token")
        if not path.exists():
            private_write(path, secrets.token_urlsafe(32).encode())
        payload = {
            "token": token(path),
            "kind": "agent",
            "audience": "control",
            "project_id": manifest["project_id"],
            "expires_at": state["expires_at"],
            "external_agent": roles[name],
        }
        CreateCredential.model_validate(payload)
        record = await command(name, "/credentials", payload, manifest["human_token_file"])
        if record.get("project_id") != manifest["project_id"] or record.get("kind") != "agent":
            raise Unknown("setup_credential_response_mismatch")
        result[name] = {
            **record,
            "token_file": str(path),
            "destination": roles[name]["destination"],
        }

    if config.get("worker"):
        from tessera_worker.kubernetes_contracts import KubernetesClusterConfig
        from tessera_worker.kubernetes_remote_service import _network, _private_json

        worker = config["worker"]
        target_path = Path(worker["target_file"])
        target = _private_json(target_path)
        evidence = _network(target["network_evidence"])
        cluster = KubernetesClusterConfig(**target["cluster"], network_none_evidence=evidence)
        if (
            cluster.context != "docker-desktop"
            or cluster.namespace_uid != worker["namespace_uid"]
            or target["output"]["pvc_uid"] != worker["pvc_uid"]
            or cluster.profile != "development-minikube-opencode-v1"
            or hashlib.sha256(target_path.read_bytes()).hexdigest() != worker["target_file_sha256"]
            or not evidence.is_fresh_for(cluster.cluster_uid, cluster.namespace_uid)
            or not re.fullmatch(r"[0-9a-f]{64}", worker["build_digest"])
            or not re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", target.get("collector_image", ""))
        ):
            raise Blocked("fresh_exact_worker_target_required")
        path = root / "worker-token"
        if not path.exists():
            private_write(path, secrets.token_urlsafe(32).encode())
        payload = {
            "token": token(path),
            "kind": "worker",
            "audience": "worker",
            "project_id": manifest["project_id"],
            "expires_at": state["expires_at"],
        }
        CreateCredential.model_validate(payload)
        record = await command("worker", "/credentials", payload, manifest["human_token_file"])
        registration = await command(
            "worker-register",
            f"/projects/{manifest['project_id']}/workers",
            {
                "worker_principal_id": record["principal_id"],
                "approved_build_identifier": "remote-dispatch-worker-v1",
                "approved_build_digest": worker["build_digest"],
                "trust_class": "development",
                "region": "local-docker-desktop",
            },
            manifest["human_token_file"],
        )
        if registration.get("worker_principal_id") != record["principal_id"]:
            raise Unknown("worker_registration_identity_mismatch")
        result["worker"] = {
            **record,
            "registration_id": registration["id"],
            "token_file": str(path),
        }

    if config.get("critic_status_bounds"):
        from tessera_control.agent_authority_models import AgentAuthorityBounds

        template = config["critic_status_bounds"]
        if roles["planner"]["destination"] != roles["critic"]["destination"]:
            raise Blocked("delegation_destination_cannot_widen")
        if template.get("budget_cents") != 0 or template.get("actions") != ["attempt:status"]:
            raise Blocked("critic_status_only_zero_budget_required")
        root_bounds = {
            **template,
            "subject_credential_id": result["planner"]["credential_id"],
            "destination": roles["planner"]["destination"],
            "actions": ["attempt:status", "authority:delegate"],
            "delegation_depth": 1,
        }
        child_bounds = {
            **template,
            "subject_credential_id": result["critic"]["credential_id"],
            "destination": roles["critic"]["destination"],
            "delegation_depth": 0,
        }
        for bounds in (root_bounds, child_bounds):
            AgentAuthorityBounds.model_validate(bounds)
            if datetime.fromisoformat(bounds["expires_at"]) > datetime.fromisoformat(
                state["expires_at"]
            ):
                raise Blocked("status_grant_cannot_outlive_setup_identity")
        issued = await command(
            "critic-status-root",
            f"/projects/{manifest['project_id']}/agent-authority-grants",
            {"bounds": root_bounds},
            manifest["human_token_file"],
        )
        delegated = await command(
            "critic-status-child",
            f"/agent-authority-grants/{issued['id']}/delegate",
            {
                "expected_revision": issued["revision"],
                "expected_epoch": issued["epoch"],
                "bounds": child_bounds,
            },
            result["planner"]["token_file"],
            roles["planner"]["destination"],
        )
        result["critic_status_grant"] = delegated

    # Peer bearer is separate from critic's control token; registry binds the real
    # planner principal/project/tenant, never identity claims in a peer message.
    peer_file = root / "peer-token"
    if not peer_file.exists():
        private_write(peer_file, secrets.token_urlsafe(32).encode())
    registry = {
        hashlib.sha256(token(peer_file).encode()).hexdigest(): {
            "tenant": result["planner"]["org_id"],
            "agent_id": result["planner"]["principal_id"],
            "project_id": manifest["project_id"],
        }
    }
    registry_path = root / "peer-identities.json"
    if registry_path.exists():
        if private_json(registry_path) != registry:
            raise Blocked("setup_peer_registry_changed")
    else:
        private_write(registry_path, registry)
    result["peer_token_file"] = str(peer_file)
    result["peer_identities_file"] = str(registry_path)
    if config.get("peer"):
        peer = config["peer"]
        peer_config = {
            "public_url": origin(peer["public_url"]),
            "control_url": origin(peer.get("control_url", manifest["control_url"])),
            "database": str(root / "critic-peer.sqlite3"),
            "agent_id": result["critic"]["principal_id"],
            "credential_id": result["critic"]["credential_id"],
            "destination": roles["critic"]["destination"],
            "model": peer["model"],
            "control_token_env": "TESSERA_JOURNEY_CRITIC_CONTROL_TOKEN",
            "identities": registry,
        }
        path = root / "critic-peer.json"
        if path.exists():
            if private_json(path) != peer_config:
                raise Blocked("peer_service_config_changed")
        else:
            private_write(path, peer_config)
        result["peer_config_file"] = str(path)
    return result


class InstalledJourney:
    def __init__(self, manifest):
        self.m = manifest
        self.observed = {}
        self.results = []

    async def http(self, client, step):
        from tessera_mcp.config import Config

        base = self.m["control_url"]
        token_ref = self.m["human_token_file"]
        token = Config(base, Path(token_ref), self.m["project_id"], "journey-human").token()
        headers = {"Authorization": "Bearer " + token}
        if step.get("idempotency_key"):
            headers["Idempotency-Key"] = step["idempotency_key"]
        path = step["path"]
        for key, item in resolve(step.get("path_parameters", {}), self.observed).items():
            if not re.fullmatch(r"[A-Za-z0-9_-]+", str(item)):
                raise Blocked("invalid_path_parameter")
            path = path.replace("{" + key + "}", str(item))
        if "{" in path or "}" in path:
            raise Blocked("unresolved_path_parameter")
        method = step["method"]
        # Streaming caps retained responses; redirects never forward authority.
        async with client.stream(
            method, base + path, headers=headers, json=resolve(step.get("body"), self.observed)
        ) as response:
            chunks, size = [], 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > 2 * 1024 * 1024:
                    raise Unknown("response_limit")
                chunks.append(chunk)
            return {"http_status": response.status_code, "body": json.loads(b"".join(chunks))}

    async def call(self, session, client, step):
        if step["via"] == "kubernetes":
            return await asyncio.to_thread(kubernetes_generation, self.m["restart_probe"])
        if step["via"] == "a2a":
            return await self.a2a(step)
        if step["via"] != "mcp":
            return await self.http(client, step)
        args = typed_arguments(step, self.observed)
        result = await session.call_tool(step["tool"], arguments=args)
        # SDK error text is never logged or substituted for a success result.
        if result.isError:
            texts = [part.text for part in result.content if part.type == "text"]
            statuses = {
                int(n)
                for text in texts
                for n in re.findall(
                    r"Control API denied or rejected request \(HTTP (403|409|410)\)", text
                )
            }
            if len(statuses) != 1:
                raise Unknown("mcp_error_not_verified_denial")
            return {"is_error": True, "http_status": statuses.pop(), "body": None}
        body = result.structuredContent
        if body is None:
            texts = [part.text for part in result.content if part.type == "text"]
            if len(texts) != 1:
                raise Unknown("unstructured_mcp_result")
            body = json.loads(texts[0])
        return {"is_error": False, "body": body}

    async def a2a(self, step):
        """Use the same actual A2A 1.0 SDK contract as tessera_control.a2a_peer."""
        import importlib.metadata
        import ssl

        import httpx
        from a2a.client import A2ACardResolver, ClientConfig, ClientFactory
        from a2a.types import a2a_pb2 as pb
        from google.protobuf.json_format import MessageToDict, ParseDict

        from tessera_mcp.config import Config

        if importlib.metadata.version("a2a-sdk") != "1.2.1":
            raise Blocked("unsupported_a2a_sdk")
        token = Config(
            self.m["critic_url"],
            Path(self.m["critic_token_file"]),
            self.m["project_id"],
            self.m["agent_destination"],
        ).token()
        async with httpx.AsyncClient(
            headers={"Authorization": "Bearer " + token},
            timeout=15,
            verify=ssl.create_default_context(cafile=self.m.get("ca_file")),
            follow_redirects=False,
            trust_env=False,
        ) as http:
            card = await A2ACardResolver(http, self.m["critic_url"]).get_agent_card()
            validate_critic_card(card, self.m)
            if step["stage"] == "critic_discovery":
                return {"body": MessageToDict(card)}
            client = ClientFactory(
                ClientConfig(
                    streaming=False, httpx_client=http, supported_protocol_bindings=["JSONRPC"]
                )
            ).create(card)
            request = ParseDict(resolve(step["request"], self.observed), pb.SendMessageRequest())
            responses = [value async for value in client.send_message(request)]
            if len(responses) != 1 or not responses[0].HasField("task"):
                raise Unknown("a2a_task_result_missing")
            return {"body": MessageToDict(responses[0])}

    async def run(self):
        validate(self.m)
        import httpx
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        from tessera_mcp.config import Config

        if self.m.get("local_setup"):
            import ssl

            async with httpx.AsyncClient(
                verify=ssl.create_default_context(cafile=self.m.get("ca_file")),
                timeout=15,
                follow_redirects=False,
                trust_env=False,
            ) as setup_client:
                prepared = await setup_local(self.m, setup_client)
            self.m["agent_token_file"] = prepared["planner"]["token_file"]
            self.m["agent_destination"] = prepared["planner"]["destination"]
            self.m["critic_token_file"] = prepared["peer_token_file"]
            self.m["critic_agent_id"] = prepared["critic"]["principal_id"]
            self.observed["setup_planner"] = prepared["planner"]
            self.observed["setup_critic"] = prepared["critic"]
            if "critic_status_grant" in prepared:
                self.observed["setup_critic_status_grant"] = prepared["critic_status_grant"]

        credentials = [
            Config(
                self.m["control_url"],
                Path(self.m[k]),
                self.m["project_id"],
                self.m["agent_destination"],
            ).token()
            for k in ("agent_token_file", "human_token_file", "critic_token_file")
        ]
        if len(set(credentials)) != 3:
            raise Blocked("separate_principals_required")
        del credentials

        # Explicit environment allowlist prevents human/peer/provider secrets reaching MCP.
        env = {k: os.environ[k] for k in ("PATH", "SYSTEMROOT") if k in os.environ}
        env.update(
            {
                "TESSERA_CONTROL_URL": self.m["control_url"],
                "TESSERA_AGENT_TOKEN_FILE": self.m["agent_token_file"],
                "TESSERA_PROJECT_ID": self.m["project_id"],
                "TESSERA_AGENT_DESTINATION": self.m["agent_destination"],
            }
        )
        if self.m.get("ca_file"):
            env["TESSERA_CONTROL_CA_FILE"] = self.m["ca_file"]
        import ssl

        tls = ssl.create_default_context(cafile=self.m.get("ca_file"))
        async with AsyncExitStack() as stack:
            # Suppress subprocess diagnostics: arbitrary server errors may contain secrets.
            err = stack.enter_context(open(os.devnull, "w"))
            read, write = await stack.enter_async_context(
                stdio_client(
                    StdioServerParameters(
                        command=sys.executable, args=["-m", "tessera_mcp.server"], env=env
                    ),
                    errlog=err,
                )
            )
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            advertised = {t.name for t in (await session.list_tools()).tools}
            if not TOOLS <= advertised:
                raise Blocked("mcp_capability_missing")
            client = await stack.enter_async_context(
                httpx.AsyncClient(verify=tls, timeout=15, follow_redirects=False, trust_env=False)
            )
            for step in self.m["steps"]:
                try:
                    if step.get("wait_until"):
                        wait = (
                            datetime.fromisoformat(step["wait_until"]) - datetime.now(UTC)
                        ).total_seconds()
                        if wait > 0:
                            await asyncio.sleep(wait)
                    if step["stage"] == "lost_reply_replay":
                        original = self.m["steps"][6]  # exact original dispatch/key
                        replay = await self.call(session, client, original)
                        if digest(replay) != digest(self.observed["agent_dispatch"]):
                            raise AssertionError("replay_changed")
                        value = replay
                    else:
                        value = await self.call(session, client, step)
                    end = asyncio.get_running_loop().time() + step.get("poll_seconds", 0)
                    while True:
                        try:
                            assertions(step, value, self.observed)
                            break
                        except AssertionError:
                            if asyncio.get_running_loop().time() >= end:
                                raise
                            await asyncio.sleep(2)
                            value = await self.call(session, client, step)
                    self.observed[step["stage"]] = value
                    self.results.append(
                        {"stage": step["stage"], "status": "passed", "code": "observed_assertions"}
                    )
                except AssertionError:
                    self.results.append(
                        {"stage": step["stage"], "status": "failed", "code": "assertion_failed"}
                    )
                    break
                except Exception:
                    self.results.append(
                        {
                            "stage": step["stage"],
                            "status": "unknown",
                            "code": "outcome_unresolved_no_retry",
                        }
                    )
                    break
        return receipt(self.m, self.results)


async def bounded_run(journey):
    try:
        async with asyncio.timeout(journey.m["max_seconds"]):
            return await journey.run()
    except Exception:
        # Do not print exception messages, request bodies, headers, or subprocess logs.
        return receipt(
            journey.m,
            journey.results
            + [
                {
                    "stage": "preflight_or_deadline",
                    "status": "unknown",
                    "code": "blocked_or_unknown_no_fallback",
                }
            ],
        )


async def setup_only(manifest):
    import ssl

    import httpx

    if manifest.get("max_seconds", 0) not in range(1, 601):
        raise Blocked("bounded_setup_deadline_required")
    origin(manifest["control_url"])
    async with (
        asyncio.timeout(manifest["max_seconds"]),
        httpx.AsyncClient(
            verify=ssl.create_default_context(cafile=manifest.get("ca_file")),
            timeout=15,
            follow_redirects=False,
            trust_env=False,
        ) as client,
    ):
        await setup_local(manifest, client)
    return {
        "status": "local_setup_completed",
        "mode": "live",
        "credential_values_logged": False,
        "jobs_submitted": False,
        "inference": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--run", action="store_true", help="Explicit bounded live execution")
    parser.add_argument(
        "--setup-agents",
        action="store_true",
        help="Bounded approved local human enrollment only; never launches services",
    )
    args = parser.parse_args()
    try:
        manifest = private_json(args.manifest)
        if args.setup_agents:
            if args.run:
                raise Blocked("choose_one_explicit_action")
            print(json.dumps(asyncio.run(setup_only(manifest))))
            return 0
        validate(manifest)
        if not args.run:
            print(json.dumps({"status": "validated_not_run", "mode": "local"}))
            return 0
        result = asyncio.run(bounded_run(InstalledJourney(manifest)))
        print(json.dumps(result, sort_keys=True))
        return 0 if result["status"] == "passed" else 1
    except Exception:
        print(json.dumps({"status": "blocked", "code": "invalid_or_unavailable_manifest"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
