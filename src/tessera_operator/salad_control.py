"""Separate delegated operator MCP backed by authoritative control HTTP metadata."""

import logging
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import SecretStr

from tessera_control.admin_models import Configuration, Identifier, SaladSetupView
from tessera_mcp.config import Config


class ControlUnavailable(ValueError):
    pass


class SaladControl:
    def __init__(self, origin, token_file, project_id, record_id):
        # Identifier validation prevents path injection; Config validates origin
        # and each operation reopens the explicit private regular token file.
        from pydantic import TypeAdapter

        adapter = TypeAdapter(Identifier)
        self.project_id = adapter.validate_python(project_id)
        self.record_id = adapter.validate_python(record_id)
        self.config = Config(origin, Path(token_file), self.project_id, "human-operator-control")
        self.path = f"/admin/projects/{self.project_id}/records/{self.record_id}"

    def _credential(self):
        try:
            return SecretStr(self.config.token())
        except Exception:
            raise ControlUnavailable("control_unavailable") from None

    def _request(self, method, suffix, *, credential, body=None, key=None):
        try:
            headers = {"Authorization": "Bearer " + credential.get_secret_value()}
            if key is not None:
                if not isinstance(key, str) or not 1 <= len(key) <= 128 or not key.strip():
                    raise ControlUnavailable("invalid_idempotency_key")
                headers["Idempotency-Key"] = key
            with httpx.Client(timeout=5, trust_env=False, follow_redirects=False) as client:
                with client.stream(
                    method,
                    self.config.endpoint.rstrip("/") + self.path + suffix,
                    headers=headers,
                    json=body,
                ) as response:
                    if response.status_code not in {200, 201}:
                        raise ControlUnavailable(
                            {
                                401: "control_authentication_required",
                                403: "control_scope_denied",
                                404: "control_record_not_found",
                                409: "control_revision_or_replay_conflict",
                            }.get(response.status_code, "control_request_failed")
                        )
                    data = bytearray()
                    for chunk in response.iter_bytes():
                        data.extend(chunk)
                        if len(data) > 65536:
                            raise ControlUnavailable("control_response_too_large")
                    import json

                    return json.loads(data)
        except ControlUnavailable:
            raise
        except Exception:
            raise ControlUnavailable("control_unavailable") from None

    def inspect(self):
        return self._inspect(self._credential())

    def _inspect(self, credential):
        try:
            view = SaladSetupView.model_validate(
                self._request("GET", "/salad-setup", credential=credential)
            )
            if (
                view.authority != "delegated"
                or view.record.id != self.record_id
                or view.record.project_id != self.project_id
            ):
                raise ControlUnavailable("separate_delegated_operator_required")
            return view.model_dump(mode="json")
        except ControlUnavailable:
            raise
        except Exception:
            raise ControlUnavailable("control_projection_invalid") from None

    def configure(self, organization, proposed_project, project, expected_revision, key):
        # One operation, one identity. Never reload the file after verifying its
        # delegated authority: replacement with an owner token must not widen POST.
        # Pin locally, not on self, so independent/concurrent operations can reload.
        credential = self._credential()
        view = self._inspect(credential)
        spec = dict(view["record"]["spec"])
        spec.update(
            salad_organization=organization,
            salad_proposed_project=proposed_project,
            salad_project=project,
        )
        try:
            configuration = Configuration.model_validate(spec).model_dump(mode="json")
            if type(expected_revision) is not int or expected_revision < 1:
                raise ValueError()
        except Exception:
            raise ControlUnavailable("invalid_setup_metadata") from None
        # Existing server command owns CAS, permission and replay checks. Never
        # silently replace expected_revision or generate a new key on retry.
        response = self._request(
            "POST",
            "/commands",
            body={
                "action": "revise",
                "expected_revision": expected_revision,
                "configuration": configuration,
            },
            key=key,
            credential=credential,
        )
        fresh = self._inspect(credential)  # Fresh HTTP readback with the same delegated identity.
        if (
            fresh["record"]["revision"] != response.get("revision")
            or fresh["record"]["spec"] != configuration
        ):
            raise ControlUnavailable("control_changed_during_readback")
        return fresh


def create_server(control):
    logging.disable(logging.CRITICAL)
    server = FastMCP("Tessera delegated Salad setup operator", log_level="CRITICAL")
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)

    def guarded(operation):
        try:
            return operation()
        except ControlUnavailable as error:
            return {"state": "blocked", "reason": str(error)}
        except Exception:
            return {"state": "blocked", "reason": "control_unavailable"}

    @server.tool(annotations=read)
    def salad_inspect() -> dict:
        """Read the exact delegated authoritative control record, no local fallback."""
        return guarded(control.inspect)

    @server.tool(annotations=read)
    def salad_readiness() -> dict:
        """Control-derived metadata only; never claims provider/deployment readiness."""
        return guarded(control.inspect)

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
    )
    def salad_configure(
        organization: str,
        expected_revision: int,
        idempotency_key: str,
        proposed_project: str = "tessera-research",
        project: str | None = None,
    ) -> dict:
        """Revise metadata only. Persist key/exact arguments before calling; reuse on retry.

        Never pass provider credentials/references. No create/approval/enable operations.
        A selected name is unverified, not a claim the project exists.
        """
        return guarded(
            lambda: control.configure(
                organization, proposed_project, project, expected_revision, idempotency_key
            )
        )

    return server
