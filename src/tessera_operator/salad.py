"""Human-only configuration MCP. No workload dispatch or credential enrollment."""

import argparse
import fcntl
import hashlib
import http.client
import json
import logging
import os
import socket
import ssl
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Literal

import certifi
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from tessera_control.salad_secret_files import PrivateFileSecretResolver

Name = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,61}[a-z0-9]$")]


class SaladConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[1] = 1
    integration: Literal["salad"] = "salad"
    organization: Name
    project: Name | None = None
    proposed_project: Name = "tessera-research"
    management_host: (
        Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9.-]{0,251}[a-z0-9]$")] | None
    ) = None
    credential_reference: Annotated[str, Field(pattern=r"^credential-ref:[a-z0-9-]{1,63}$")]
    credential_version: Annotated[int, Field(ge=1, le=2**31 - 1)]


def _digest(config):
    return hashlib.sha256(config.model_dump_json().encode()).hexdigest()


def _setup_state(config):
    if config is None:
        return "unconfigured"
    return "project_creation_requires_portal" if config.project is None else "configured"


def _portal_action(config):
    if config is None or config.project is not None:
        return None
    return {
        "url": "https://docs.salad.com/general/tutorials/account-setup",
        "proposed_project": config.proposed_project,
        "project_created": False,
        "steps": [
            f"Open the Salad Portal and select organization {config.organization}.",
            f"Create a project using proposed name {config.proposed_project}; "
            "do not deploy workloads.",
            "Return the actual project name to salad_discover_projects(candidate_project=...).",
            "After a successful project read, save that name with salad_configure "
            "and the inspected revision.",
        ],
    }


def _directory(path):
    """Open explicit private directory, refusing symlinks in every component."""
    if not Path(path).is_absolute():
        raise ValueError("private absolute directory required")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in Path(path).parts[1:]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        PrivateFileSecretResolver._private(os.fstat(fd), directory=True)
        return os.dup(fd)
    finally:
        os.close(fd)


class ConfigurationStore:
    """Reusable integration convention: private files, fixed schema, CAS, atomic replace.

    One operator owns this directory. No arbitrary file paths come from MCP tools.
    Other providers need their own reviewed schema and tool registration.
    """

    def __init__(self, directory):
        self.fd = _directory(directory)

    def close(self):
        os.close(self.fd)

    @contextmanager
    def locked(self):
        PrivateFileSecretResolver._private(os.fstat(self.fd), directory=True)
        fd = os.open(".salad.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=self.fd)
        try:
            PrivateFileSecretResolver._private(os.fstat(fd))
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def read(self):
        try:
            fd = os.open("salad.json", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self.fd)
        except FileNotFoundError:
            return None
        try:
            PrivateFileSecretResolver._private(os.fstat(fd))
            if os.fstat(fd).st_size > 4096:
                raise ValueError("configuration unavailable")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                return SaladConfiguration.model_validate_json(stream.read(4097))
        finally:
            os.close(fd)

    def configure(self, config, expected_revision):
        with self.locked():
            previous = self.read()
            revision = None if previous is None else _digest(previous)
            if revision != expected_revision:
                return {"state": "blocked", "reason": "stale_revision"}
            temporary = ".salad-" + uuid.uuid4().hex
            fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=self.fd)
            try:
                with os.fdopen(fd, "wb", closefd=False) as stream:
                    stream.write(config.model_dump_json().encode())
                    stream.flush()
                    os.fsync(fd)
                os.replace(temporary, "salad.json", src_dir_fd=self.fd, dst_dir_fd=self.fd)
                os.fsync(self.fd)
            finally:
                os.close(fd)
                try:
                    os.unlink(temporary, dir_fd=self.fd)
                except FileNotFoundError:
                    pass
            return {
                "state": _setup_state(config),
                "revision": _digest(config),
                "enabled": False,
                "next_action": _portal_action(config),
            }


def _provider_get(path, secret):
    """Exactly one GET to a fixed origin/path; no redirects, retries or proxy env."""
    connection = None
    try:
        if not all(33 <= byte <= 126 for byte in secret._buffer):
            return "credential_unavailable", None
        # The existing httpx dependency supplies certifi. Explicit Mozilla roots
        # avoid Python installs with no default CA paths; TLS/hostname checks stay on.
        context = ssl.create_default_context(cafile=certifi.where())
        connection = http.client.HTTPSConnection(
            "api.salad.com", port=443, timeout=3, context=context
        )
        deadline = time.monotonic() + 3
        connection.request(
            "GET",
            path,
            headers={"Salad-Api-Key": secret._buffer.decode("ascii"), "Accept": "application/json"},
        )
        response = connection.getresponse()
        if 300 <= response.status < 400:
            return "redirect_denied", None
        if response.status != 200:
            return {
                401: "credential_rejected",
                403: "provider_access_denied",
                404: "scope_not_found",
                408: "provider_timeout",
                429: "provider_rate_limited",
            }.get(response.status, "provider_http_error"), None
        raw = bytearray()
        while len(raw) <= 65536:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "provider_timeout", None
            if connection.sock is not None:
                connection.sock.settimeout(remaining)
            chunk = response.read1(min(4096, 65537 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        if len(raw) > 65536 or secret.contains_in(raw):
            return "malformed_response", None
        data = json.loads(raw)
        if not isinstance(data, dict):
            return "malformed_response", None
        return "readable", data
    except ssl.SSLError:
        return "provider_tls_failed", None
    except socket.gaierror:
        return "provider_dns_failed", None
    except TimeoutError:
        return "provider_timeout", None
    except Exception:
        return "provider_read_failed", None
    finally:
        if connection is not None:
            connection.close()


def provider_project(config, secret):
    reason, data = _provider_get(
        f"/api/public/organizations/{config.organization}/projects/{config.project}/containers",
        secret,
    )
    if reason != "readable":
        return reason
    if not isinstance(data.get("items"), list):
        return "malformed_response"
    for item in data["items"]:
        if not isinstance(item, dict) or (
            item.get("organization_name", config.organization) != config.organization
            or item.get("project_name", config.project) != config.project
        ):
            return "project_identity_mismatch"
    return "project_readable"


class ProjectRead(BaseModel):
    organization: Name
    project: Name


class ScopeReference(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    credential_reference: Annotated[str, Field(pattern=r"^credential-ref:[a-z0-9-]{1,63}$")]
    credential_version: Annotated[int, Field(ge=1, le=2**31 - 1)]
    candidate_project: Name | None = None


def create_server(store, credential_directory):
    server = FastMCP("Tessera Salad human operator", log_level="CRITICAL")
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)

    def guarded(operation):
        try:
            return operation()
        except Exception:
            # Never propagate Pydantic, filesystem, provider or credential exception text.
            return {"state": "blocked", "reason": "private_configuration_unavailable"}

    @server.tool(annotations=read)
    def salad_inspect() -> dict:
        """Read private operator metadata only; no credentials or provider calls."""

        def inspect():
            config = store.read()
            return {
                "state": _setup_state(config),
                "revision": None if config is None else _digest(config),
                "configuration": None if config is None else config.model_dump(),
                "enabled": False,
                "workload_operations": [],
                "next_action": _portal_action(config),
            }

        return guarded(inspect)

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
    )
    def salad_configure(
        organization: str,
        credential_reference: str,
        credential_version: int,
        project: str | None = None,
        management_host: str | None = None,
        proposed_project: str = "tessera-research",
        expected_revision: str | None = None,
    ) -> dict:
        """Persist operator metadata with compare-and-swap; no enrollment/deploy/provider effects.

        Pass only an existing opaque credential-ref, NEVER a credential value.
        management_host is selected deployment metadata, not an SSH or network target.
        """
        return guarded(
            lambda: store.configure(
                SaladConfiguration(
                    organization=organization,
                    project=project,
                    proposed_project=proposed_project,
                    management_host=management_host,
                    credential_reference=credential_reference,
                    credential_version=credential_version,
                ),
                expected_revision,
            )
        )

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)
    )
    def salad_readiness(validate_provider: bool = False) -> dict:
        """Default is metadata-only. Explicit true reads the exact Salad project once.

        Success is account/project read access, NOT deployment, GPU or execution readiness.
        """

        def readiness():
            config = store.read()
            result = {
                "state": _setup_state(config),
                "revision": None if config is None else _digest(config),
                "provider": "unrun",
                "deployment": "unverified",
                "guardian_custody": "unverified",
                "execution_enabled": False,
                "account": "unrun",
                "project": "requires_portal" if config and config.project is None else "unverified",
                "billing": "no_cash_hard_cap_unverified",
                "control_integration": "not_connected",
                "next_action": _portal_action(config),
            }
            if config is None or not validate_provider:
                return result
            resolver = PrivateFileSecretResolver(credential_directory)
            try:
                metadata = resolver.metadata(config.credential_reference)
                if (
                    metadata.version != config.credential_version
                    or metadata.revoked
                    or metadata.expires_at <= time.time() + 3
                ):
                    result["provider"] = "credential_unavailable"
                    return result
                with resolver.resolve(
                    config.credential_reference, config.credential_version
                ) as secret:
                    if config.project is None:
                        reason, _ = _provider_get(
                            f"/api/public/organizations/{config.organization}/quotas", secret
                        )
                        result["provider"] = (
                            "organization_readable" if reason == "readable" else reason
                        )
                        result["account"] = result["provider"]
                    else:
                        result["provider"] = provider_project(config, secret)
                        result["project"] = result["provider"]
            finally:
                resolver.close()
            return result

        return guarded(readiness)

    @server.tool(
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)
    )
    def salad_discover_projects(
        credential_reference: str, credential_version: int, candidate_project: str | None = None
    ) -> dict:
        """Validate luxai scope; the public API CANNOT enumerate projects.

        No candidate: one GET of luxai quotas. Candidate: one GET of its container
        collection. No raw quota/container data is returned. No key values accepted.
        """

        def discover():
            reference = ScopeReference(
                credential_reference=credential_reference,
                credential_version=credential_version,
                candidate_project=candidate_project,
            )
            result = {
                "organization": "luxai",
                "project_discovery": "unsupported_by_public_api",
                "candidate_project": reference.candidate_project,
                "provider": "credential_unavailable",
                "execution_enabled": False,
            }
            resolver = PrivateFileSecretResolver(credential_directory)
            try:
                metadata = resolver.metadata(reference.credential_reference)
                if (
                    metadata.version != reference.credential_version
                    or metadata.revoked
                    or metadata.expires_at <= time.time() + 3
                ):
                    return result
                with resolver.resolve(
                    reference.credential_reference, reference.credential_version
                ) as secret:
                    if reference.candidate_project is None:
                        reason, _ = _provider_get("/api/public/organizations/luxai/quotas", secret)
                        result["provider"] = (
                            "organization_readable" if reason == "readable" else reason
                        )
                    else:
                        config = ProjectRead(
                            organization="luxai", project=reference.candidate_project
                        )
                        result["provider"] = provider_project(config, secret)
            finally:
                resolver.close()
            return result

        return guarded(discover)

    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description="Separate human-only Salad operator stdio MCP")
    parser.add_argument("--state-directory")
    parser.add_argument("--credential-directory")
    parser.add_argument("--control-url")
    parser.add_argument("--control-project-id")
    parser.add_argument("--control-record-id")
    parser.add_argument("--control-token-file")
    args = parser.parse_args(argv)
    logging.disable(logging.CRITICAL)
    store = None
    try:
        control_options = (
            args.control_url,
            args.control_project_id,
            args.control_record_id,
            args.control_token_file,
        )
        if any(control_options):
            if not all(control_options) or args.state_directory or args.credential_directory:
                return 2  # No ambiguous backend selection or local authority fallback.
            from .salad_control import SaladControl
            from .salad_control import create_server as control_server

            control = SaladControl(
                args.control_url,
                args.control_token_file,
                args.control_project_id,
                args.control_record_id,
            )
            control_server(control).run(transport="stdio")
            return 0
        if not args.state_directory or not args.credential_directory:
            return 2
        store = ConfigurationStore(args.state_directory)
        create_server(store, args.credential_directory).run(transport="stdio")
    except Exception:
        return 2
    finally:
        if store is not None:
            store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
