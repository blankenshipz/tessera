"""A2A 1.0 JSON-RPC reference exchange using the official 1.2.1 SDK.

This peer exchanges durable task/result references, not model reasoning or job
execution. Optional generated references require central public-output registration
and current recipient disclosure; planner consumption requires its own authenticated
ACK. Execution authority is checked/consumed by Tessera, never this ledger.
"""

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from a2a.server.context import ServerCallContext
from a2a.server.request_handlers import RequestHandler
from a2a.server.routes.agent_card_routes import create_agent_card_routes
from a2a.server.routes.common import ServerCallContextBuilder
from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
from a2a.types import a2a_pb2 as pb
from a2a.utils.errors import (
    InvalidParamsError,
    TaskNotCancelableError,
    TaskNotFoundError,
    UnsupportedOperationError,
)
from google.protobuf.json_format import MessageToDict, ParseDict
from starlette.applications import Starlette
from starlette.responses import JSONResponse

from .a2a_store import (
    A2AStore,
    PeerConflict,
    PeerNotFound,
    generated_reference,
    validate_generated_pins,
)

REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._/-]{0,199}$")
IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{1,200}$")
BINDING_KEYS = {
    "variant_id",
    "expected_variant_revision",
    "expected_design_revision",
    "execution_grant_id",
    "expected_execution_grant_revision",
    "expected_execution_epoch",
    "task_id",
    "expected_task_revision",
    "plan_digest",
    "target_digest",
}


def envelope_from(message):
    value = MessageToDict(message)
    if len(value.get("parts", [])) != 1 or set(value["parts"][0]) != {"data"}:
        raise InvalidParamsError("one structured reference part required")
    body = value["parts"][0]["data"]
    if set(body) not in (
        {"operation", "reference", "authority"},
        {"operation", "reference", "authority", "generated"},
    ):
        raise InvalidParamsError("closed reference envelope required")
    if (
        body["operation"] not in {"submit", "result", "consume"}
        or not isinstance(body["reference"], str)
        or not REFERENCE.fullmatch(body["reference"])
    ):
        raise InvalidParamsError("unsupported operation or reference")
    if body["operation"] == "consume" and "generated" not in body:
        raise InvalidParamsError("consumption requires registered generated output")
    if "generated" in body:
        try:
            validate_generated_pins(body["generated"])
        except PeerConflict as exc:
            raise InvalidParamsError(str(exc)) from exc
        if body["reference"] != generated_reference(body["generated"]):
            raise InvalidParamsError("generated reference must be the registered artifact")
    fence = body["authority"]
    if not isinstance(fence, dict) or set(fence) != {"grant_id", "revision", "epoch", "binding"}:
        raise InvalidParamsError("authority fence required")
    if not isinstance(fence["grant_id"], str) or not IDENTIFIER.fullmatch(fence["grant_id"]):
        raise InvalidParamsError("invalid grant reference")
    for key in ("revision", "epoch"):
        if (
            isinstance(fence[key], bool)
            or not isinstance(fence[key], (int, float))
            or fence[key] < 1
            or int(fence[key]) != fence[key]
        ):
            raise InvalidParamsError("invalid authority revision")
    if not isinstance(fence["binding"], dict) or set(fence["binding"]) != BINDING_KEYS:
        raise InvalidParamsError("exact authority binding required")
    for key, value in fence["binding"].items():
        if key.startswith("expected_"):
            valid = (
                not isinstance(value, bool)
                and isinstance(value, (int, float))
                and value >= 1
                and int(value) == value
            )
        elif key.endswith("digest"):
            valid = isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value)
        else:
            valid = isinstance(value, str) and IDENTIFIER.fullmatch(value)
        if not valid:
            raise InvalidParamsError("invalid authority binding")
    if len(json.dumps(body)) > 16384:
        raise InvalidParamsError("reference envelope too large")
    return body


class ControlAuthorityVerifier:
    """Revalidate delegated bounds using only the recipient's agent credential."""

    def __init__(self, config):
        self.config = config

    async def __call__(self, identity, fence):
        config = self.config
        token = os.environ[config["control_token_env"]]
        async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False) as client:
            response = await client.get(
                config["control_url"].rstrip("/") + "/agent-authority-grants/" + fence["grant_id"],
                headers={
                    "Authorization": "Bearer " + token,
                    "X-Agent-Destination": config["destination"],
                },
            )
        if response.status_code != 200:
            raise InvalidParamsError("delegated authority unavailable")
        grant = response.json()
        bounds = grant.get("bounds", {})
        expires = datetime.fromisoformat(bounds.get("expires_at", "1970-01-01T00:00:00+00:00"))
        if (
            grant.get("status") != "active"
            or grant.get("effective_status") != "active"
            or not grant.get("parent_id")
            or grant.get("issuer_id") != identity["agent_id"]
            or grant.get("org_id") != identity["tenant"]
            or grant.get("revision") != fence["revision"]
            or grant.get("epoch") != fence["epoch"]
            or grant.get("project_id") != identity["project_id"]
            or bounds.get("subject_credential_id") != config["credential_id"]
            or bounds.get("destination") != config["destination"]
            or bounds.get("model") != config["model"]
            or bounds.get("delegation_depth") != 0
            or bounds.get("actions") != ["attempt:status"]
            or bounds.get("budget_cents") != 0
            or fence["binding"] not in bounds.get("bindings", [])
            or expires.tzinfo is None
            or expires <= datetime.now(UTC)
        ):
            raise InvalidParamsError("delegated authority fence denied")


class RegisteredGeneratedOutputVerifier:
    """Read authoritative registered public metadata and current recipient disclosure.

    The collector attests registration, not model reasoning or scientific validity.
    No producer-supplied booleans, text, paths or URLs are accepted as evidence.
    """

    def __init__(self, config):
        self.config = config

    async def __call__(self, sender, fence, pins):
        from .agent_provenance_harness_models import PublicHarnessMetadata

        validate_generated_pins(pins)
        if pins["task_id"] != fence["binding"]["task_id"]:
            raise InvalidParamsError("generated task differs from narrowed delegation")
        config = self.config
        if sender["agent_id"] == config["agent_id"]:
            raise InvalidParamsError("critic and receiving planner identities must differ")
        headers = {
            "Authorization": "Bearer " + os.environ[config["control_token_env"]],
            "X-Agent-Destination": config["destination"],
        }

        async def read(client, path, params=None):
            async with client.stream(
                "GET", config["control_url"].rstrip("/") + path, headers=headers, params=params
            ) as response:
                if response.status_code != 200:
                    raise InvalidParamsError("registered public output unavailable")
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(chunk) > 262144 - len(data):
                        raise InvalidParamsError("registered output metadata response bound")
                    data.extend(chunk)
                return json.loads(data)

        try:
            async with httpx.AsyncClient(
                timeout=5, follow_redirects=False, trust_env=False
            ) as client:
                if pins.get("family") == "research-generated-v1":
                    metadata = await read(
                        client,
                        "/generated-outputs/" + pins["output_id"] + "/metadata",
                        {"disclosure_id": pins["disclosure_id"]},
                    )
                    from .generated_output_models import OutputPins

                    output_pins = OutputPins.model_validate(metadata.get("pins")).model_dump(
                        mode="json"
                    )
                    if (
                        pins["actor_id"] != sender["agent_id"]
                        or pins["org_id"] != sender["tenant"]
                        or pins["project_id"] != sender["project_id"]
                        or metadata.get("id") != pins["output_id"]
                        or metadata.get("artifact_id") != pins["output_id"]
                        or metadata.get("actor_id") != pins["actor_id"]
                        or metadata.get("credential_id") != pins["critic_credential_id"]
                        or metadata.get("org_id") != pins["org_id"]
                        or metadata.get("project_id") != pins["project_id"]
                        or metadata.get("content_digest") != pins["content_digest"]
                        or metadata.get("sha256") != pins["content_digest"].removeprefix("sha256:")
                        or metadata.get("grant_id") != pins["registration_grant_id"]
                        or metadata.get("expected_revision") != pins["registration_revision"]
                        or metadata.get("output_kind") != "critic_review"
                        or metadata.get("visibility") != "public"
                        or metadata.get("source_event_id") != pins["source_event_id"]
                        or metadata.get("source_sequence") != pins["source_sequence"]
                        or metadata.get("custody_state") != "human_admitted_public"
                        or metadata.get("attribution")
                        != "registered_self_reported_generated_output"
                        or metadata.get("execution_established") is not False
                        or metadata.get("scientific_validity") != "not_assessed"
                        or "content_base64" in metadata
                        or type(metadata.get("size")) is not int
                        or not 1 <= metadata["size"] <= 65536
                        or any(output_pins[key] != pins[key] for key in output_pins)
                    ):
                        raise InvalidParamsError("registered research-generated output pins differ")
                    # The authenticated metadata route rechecks the separately
                    # human-issued recipient disclosure and integrity. No worker
                    # manifest/Job/custody identity or collector row is fabricated.
                    if "capture_record_id" in pins:
                        projection = await read(
                            client,
                            "/projects/" + sender["project_id"] + "/agent-notebook",
                            {
                                "after_sequence": int(pins["capture_sequence"]) - 1,
                                "limit": 1,
                                "max_bytes": 262144,
                            },
                        )
                        captured = projection.get("provenance", [])
                        if len(captured) != 1:
                            raise InvalidParamsError("exact registered public capture required")
                        record = captured[0]
                        harness = PublicHarnessMetadata.model_validate(
                            record.get("harness")
                        ).model_dump(mode="json")
                        ref = record.get("generated_output", {})
                        if (
                            record.get("record_id") != pins["capture_record_id"]
                            or record.get("sequence") != pins["capture_sequence"]
                            or record.get("actor_principal_id") != pins["actor_id"]
                            or record.get("credential_id") != pins["critic_credential_id"]
                            or record.get("model_id") != pins["model_id"]
                            or record.get("content_digest") != pins["content_digest"]
                            or record.get("kind") != "attempt"
                            or harness["public_event"] != "generated_output"
                            or harness["capture_state"] != "bounded_public_stream"
                            or any(
                                harness[key] != pins[key]
                                for key in (
                                    "task_id",
                                    "context_id",
                                    "run_id",
                                    "session_id",
                                    "harness_instance_id",
                                    "context_digest",
                                    "skill_digest",
                                    "config_digest",
                                    "source_event_id",
                                    "source_sequence",
                                )
                            )
                            or any(
                                ref.get(key) != item
                                for key, item in {
                                    "family": "generated_public_research_output_v1",
                                    "output_id": pins["output_id"],
                                    "sha256": metadata["sha256"],
                                    "output_kind": "critic_review",
                                    "writer_actor_id": pins["actor_id"],
                                    "writer_credential_id": pins["critic_credential_id"],
                                    "writer_org_id": pins["org_id"],
                                }.items()
                            )
                            or not ref.get("disclosure_id")
                        ):
                            raise InvalidParamsError("typed generated public capture differs")
                    return dict(pins)
                notebook = await read(
                    client,
                    "/projects/" + sender["project_id"] + "/agent-notebook",
                    {
                        "after_sequence": int(pins["collector_sequence"]) - 1,
                        "limit": 1,
                        "max_bytes": 262144,
                    },
                )
                records = notebook.get("provenance", [])
                if len(records) != 1:
                    raise InvalidParamsError("exact registered public output required")
                record = records[0]
                harness = PublicHarnessMetadata.model_validate(record.get("harness")).model_dump(
                    mode="json"
                )
                refs = record.get("references", {})
                if (
                    record.get("record_id") != pins["record_id"]
                    or record.get("sequence") != pins["collector_sequence"]
                    or record.get("actor_principal_id") != sender["agent_id"]
                    or record.get("credential_id") != pins["critic_credential_id"]
                    or record.get("model_id") != pins["model_id"]
                    or record.get("kind") != "attempt"
                    or record.get("outcome") != "succeeded"
                    or record.get("content_digest") != pins["content_digest"]
                    or harness["public_event"] != "generated_output"
                    or harness["capture_state"] != "bounded_public_stream"
                    or any(
                        harness[key] != pins[key]
                        for key in (
                            "task_id",
                            "context_id",
                            "run_id",
                            "session_id",
                            "harness_instance_id",
                            "context_digest",
                            "skill_digest",
                            "config_digest",
                            "source_event_id",
                            "source_sequence",
                        )
                    )
                    or any(
                        refs.get(key) != value
                        for key, value in {
                            "task_id": pins["task_id"],
                            "context_id": pins["context_id"],
                            "artifact_id": pins["artifact_id"],
                            "operation_id": pins["operation_id"],
                            "output_manifest_id": pins["manifest_id"],
                        }.items()
                    )
                ):
                    raise InvalidParamsError("registered generated output pins differ")
                context = await read(client, "/agent-contexts/" + pins["recipient_context_id"])
                if (
                    context.get("id") != pins["recipient_context_id"]
                    or context.get("project_id") != sender["project_id"]
                    or context.get("credential_id") != config["credential_id"]
                    or context.get("destination") != config["destination"]
                    or context.get("disclosure_approved") is not True
                    or context.get("artifact_id") != pins["artifact_id"]
                    or context.get("manifest_id") != pins["manifest_id"]
                    or context.get("expected_manifest_digest") != pins["manifest_digest"]
                    or context.get("sha256") != pins["content_digest"].removeprefix("sha256:")
                    or type(context.get("size")) is not int
                    or not 0 <= context["size"] <= 65536
                ):
                    raise InvalidParamsError("receiving planner artifact disclosure differs")
                # Never persist or send returned content_base64/body. The central
                # context route already validates current custody, placement/hash.
                return dict(pins)
        except InvalidParamsError:
            raise
        except Exception as exc:
            raise InvalidParamsError("registered public output verification unavailable") from exc


class IdentityContext(ServerCallContextBuilder):
    def build(self, request):
        return ServerCallContext(
            state={
                "identity": request.scope["peer_identity"],
                "headers": {"a2a-version": request.headers.get("a2a-version", "")},
            }
        )


class PeerAuthentication:
    def __init__(self, app, identities):
        self.app, self.identities = app, identities

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"] != "/.well-known/agent-card.json":
            headers = dict(scope["headers"])
            bearer = headers.get(b"authorization", b"").decode("latin1")
            digest = hashlib.sha256(bearer.removeprefix("Bearer ").encode()).hexdigest()
            identity = self.identities.get(digest) if bearer.startswith("Bearer ") else None
            if identity is None:
                await JSONResponse({"error": "peer authentication required"}, status_code=401)(
                    scope, receive, send
                )
                return
            scope["peer_identity"] = identity
            if scope["method"] == "POST":
                chunks, size = [], 0
                while True:
                    event = await receive()
                    if event["type"] == "http.disconnect":
                        return
                    chunk = event.get("body", b"")
                    size += len(chunk)
                    if size > 32768:
                        await JSONResponse(
                            {"error": "reference request too large"}, status_code=413
                        )(scope, receive, send)
                        return
                    chunks.append(chunk)
                    if not event.get("more_body", False):
                        break
                delivered = False

                async def bounded_receive():
                    nonlocal delivered
                    if delivered:
                        return await receive()
                    delivered = True
                    return {"type": "http.request", "body": b"".join(chunks), "more_body": False}

                await self.app(scope, bounded_receive, send)
                return
        await self.app(scope, receive, send)


class PeerHandler(RequestHandler):
    def __init__(self, store, verifier, config=None, generated_verifier=None):
        self.store, self.verifier = store, verifier
        self.config = config or {}
        self.generated_verifier = generated_verifier or RegisteredGeneratedOutputVerifier(
            self.config
        )

    def origin(self, identity):
        if not all(key in self.config for key in ("agent_id", "credential_id", "destination")):
            return None  # isolated compatibility tests with synthetic verifier only
        return {
            "receiver_peer_id": self.config["agent_id"],
            "credential_id": self.config["credential_id"],
            "destination": self.config["destination"],
            "project_id": identity["project_id"],
        }

    async def deliver(self, identity, task):
        if self.config.get("deliver_provenance"):
            try:
                await asyncio.to_thread(
                    drain_peer_outbox,
                    self.config,
                    identity,
                    task["id"],
                    self.config.get("provenance_drain_limit", 10),
                )
            except Exception:
                # Protocol acceptance stands; local pending completeness remains
                # truthful even if no secure collector credential is available.
                pass

    @staticmethod
    def identity(params, context):
        identity = context.state["identity"]
        if params.tenant and params.tenant != identity["tenant"]:
            raise TaskNotFoundError("task not found")
        return identity

    async def on_message_send(self, params, context):
        identity = self.identity(params, context)
        if not params.message.message_id or params.message.role != pb.ROLE_USER:
            raise InvalidParamsError("user message identifier required")
        envelope = envelope_from(params.message)
        try:
            authority_identity = identity
            consumption = envelope["operation"] == "consume"
            if consumption:
                original = self.store.consumption_task(
                    identity["tenant"], identity["agent_id"], params.message.task_id
                )
                origin = original["metadata"]["peer_provenance_origin"]
                if (
                    identity["project_id"] != origin["project_id"]
                    or self.origin(identity) != origin
                ):
                    raise PeerNotFound("task not found")
                authority_identity = {
                    **identity,
                    "agent_id": original["metadata"]["authenticated_sender_id"],
                }
            await self.verifier(authority_identity, envelope["authority"])
            receipt = None
            if "generated" in envelope:
                receipt = await self.generated_verifier(
                    authority_identity, envelope["authority"], envelope["generated"]
                )
            if consumption:
                task = self.store.consume(
                    identity["tenant"],
                    identity["agent_id"],
                    params.message.message_id,
                    params.message.task_id,
                    envelope,
                    generated_receipt=receipt,
                )
            else:
                task = self.store.send(
                    identity["tenant"],
                    identity["agent_id"],
                    params.message.message_id,
                    envelope,
                    params.message.task_id,
                    provenance_origin=self.origin(identity),
                    generated_receipt=receipt,
                )
        except PeerNotFound as exc:
            raise TaskNotFoundError("task not found") from exc
        except PeerConflict as exc:
            raise InvalidParamsError(str(exc)) from exc
        except ValueError as exc:
            raise InvalidParamsError("content-free opaque message identity required") from exc
        delivery_identity = {
            **identity,
            "agent_id": task["metadata"].get("authenticated_sender_id", identity["agent_id"]),
        }
        await self.deliver(delivery_identity, task)
        return ParseDict(task, pb.Task())

    async def on_get_task(self, params, context):
        identity = self.identity(params, context)
        try:
            try:
                task = self.store.get(identity["tenant"], identity["agent_id"], params.id)
            except PeerNotFound:
                task = self.store.consumption_task(
                    identity["tenant"], identity["agent_id"], params.id
                )
                if self.origin(identity) != task["metadata"]["peer_provenance_origin"]:
                    raise PeerNotFound("task not found") from None
        except PeerNotFound as exc:
            raise TaskNotFoundError("task not found") from exc
        if params.HasField("history_length"):
            task["history"] = (
                task["history"][-params.history_length :] if params.history_length else []
            )
        return ParseDict(task, pb.Task())

    async def on_cancel_task(self, params, context):
        identity = self.identity(params, context)
        try:
            task = self.store.cancel(
                identity["tenant"],
                identity["agent_id"],
                params.id,
                provenance_origin=self.origin(identity),
            )
        except PeerNotFound as exc:
            raise TaskNotFoundError("task not found") from exc
        except PeerConflict as exc:
            raise TaskNotCancelableError(str(exc)) from exc
        await self.deliver(identity, task)
        return ParseDict(task, pb.Task())

    async def on_list_tasks(self, params, context):
        self.identity(params, context)
        # Do not advertise incomplete filtering/token semantics as ListTasks support.
        raise UnsupportedOperationError("task listing not supported; retain task identifiers")

    async def unsupported(self, params, context):
        raise UnsupportedOperationError("operation not supported")

    async def unsupported_stream(self, params, context):
        raise UnsupportedOperationError("streaming not supported")
        yield  # pragma: no cover

    on_message_send_stream = unsupported_stream
    on_subscribe_to_task = unsupported_stream
    on_create_task_push_notification_config = unsupported
    on_get_task_push_notification_config = unsupported
    on_list_task_push_notification_configs = unsupported
    on_delete_task_push_notification_config = unsupported
    on_get_extended_agent_card = unsupported


def create_peer(config, verifier=None, generated_verifier=None):
    # SDK debug/validation logs can contain raw request bodies. This service's
    # durable ledger deliberately records only validated reference envelopes.
    logging.getLogger("a2a.server.routes.jsonrpc_dispatcher").disabled = True
    for key in ("public_url", "control_url"):
        url = urlsplit(config[key])
        if (
            url.username
            or url.password
            or url.query
            or url.fragment
            or url.scheme not in {"http", "https"}
        ):
            raise ValueError("explicit credential-free service URL required")
        if url.scheme == "http" and url.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("off-host services require TLS")
    card = pb.AgentCard(
        name=config["agent_id"],
        version="1.0.0",
        description="Governed reference exchange; no model execution or scientific verification",
        supported_interfaces=[
            pb.AgentInterface(
                url=config["public_url"], protocol_binding="JSONRPC", protocol_version="1.0"
            )
        ],
        capabilities=pb.AgentCapabilities(streaming=False, push_notifications=False),
        default_input_modes=["application/json"],
        default_output_modes=["application/json"],
        skills=[
            pb.AgentSkill(
                id="reference-exchange",
                name="Reference exchange",
                description=(
                    "Exchange untrusted result references under delegated status-only authority"
                ),
                tags=["reference"],
            ),
            pb.AgentSkill(
                id="registered-public-output",
                name="Registered public output reference",
                description=(
                    "Deliver an already registered disclosed public artifact reference; "
                    "explicit receiving-planner ACK. No model execution or scientific verification."
                ),
                tags=["reference", "registered", "public"],
            ),
        ],
        security_schemes={
            "peerBearer": pb.SecurityScheme(
                http_auth_security_scheme=pb.HTTPAuthSecurityScheme(scheme="bearer")
            )
        },
        security_requirements=[pb.SecurityRequirement(schemes={"peerBearer": pb.StringList()})],
    )
    handler = PeerHandler(
        A2AStore(Path(config["database"])),
        verifier or ControlAuthorityVerifier(config),
        config,
        generated_verifier,
    )
    app = Starlette(
        routes=create_agent_card_routes(card)
        + create_jsonrpc_routes(handler, "/", IdentityContext())
    )
    return PeerAuthentication(app, config["identities"])


def drain_peer_outbox(config, identity, task_id, limit=100):
    """Installed reference-peer metadata delivery, independent of reasoning profiles."""
    from tessera_agent.a2a_reasoning_bridge import drain_outbox
    from tessera_agent.provenance_client import ProvenanceClient

    if identity not in config["identities"].values():
        raise ValueError("operator selector is not an enrolled peer identity")
    store = A2AStore(Path(config["database"]))
    task = store.get(identity["tenant"], identity["agent_id"], task_id)
    expected = {
        "receiver_peer_id": config["agent_id"],
        "credential_id": config["credential_id"],
        "project_id": identity["project_id"],
        "destination": config["destination"],
    }
    if task["metadata"].get("peer_provenance_origin") != expected:
        raise ValueError("task has no matching receiving-peer provenance origin")
    mapping = {
        "tenant": identity["tenant"],
        "sender_id": identity["agent_id"],
        "task_id": task_id,
        "project_id": identity["project_id"],
    }
    with ProvenanceClient(
        config["control_url"], os.environ[config["control_token_env"]], config["destination"]
    ) as client:
        return drain_outbox(store, mapping, client, limit=limit)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", default="serve", choices=("serve", "drain-outbox"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8771)
    parser.add_argument("--task-id")
    parser.add_argument("--sender-id")
    parser.add_argument("--tenant")
    parser.add_argument("--project-id")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--deliver-provenance", action="store_true")
    args = parser.parse_args(argv)
    from tessera_agent.remote_reasoning_store import private_file

    config = json.loads(private_file(args.config))
    if args.command == "drain-outbox":
        if not all((args.task_id, args.sender_id, args.tenant, args.project_id)):
            parser.error("drain-outbox requires --task-id --sender-id --tenant --project-id")
        result = drain_peer_outbox(
            config,
            {"agent_id": args.sender_id, "tenant": args.tenant, "project_id": args.project_id},
            args.task_id,
            args.limit,
        )
        print(json.dumps(result))
        return 0 if result["outbox_fully_acknowledged"] else 2
    if args.deliver_provenance:
        config["deliver_provenance"] = True
    import uvicorn

    uvicorn.run(
        create_peer(config),
        host=args.host,
        port=args.port,
        access_log=False,
    )


if __name__ == "__main__":
    raise SystemExit(main())
