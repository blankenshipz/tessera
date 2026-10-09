"""Fixed authenticated HTTP and MCP stdio transports; no discovery or inference fallback."""

import base64
import fcntl
import json
import os
import ssl
import stat
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

from .remote_reasoning_store import private_file
from .research_consumer_store import ConsumerBlocked


def observer_endpoint(origin, credential_file, *, ca_file=None, destination=None, basic=False):
    from tessera_mcp.harness_observer import ObserverHttpEndpoint

    class MappedObserverEndpoint(ObserverHttpEndpoint):
        def __init__(self):
            token = private_file(Path(credential_file)).decode().strip()
            if not token or len(token) > 8192 or any(c.isspace() for c in token):
                raise ConsumerBlocked("observer_credential_reference_invalid")
            authorization = "Bearer " + token
            if basic:
                authorization = "Basic " + base64.b64encode(("opencode:" + token).encode()).decode()
            super().__init__(origin, authorization)
            self._client.close()
            headers = {"Authorization": authorization}
            if destination:
                headers["X-Agent-Destination"] = destination
            verify = ssl.create_default_context(cafile=str(ca_file)) if ca_file else True
            self._client = httpx.Client(
                base_url=self.origin,
                headers=headers,
                verify=verify,
                trust_env=False,
                follow_redirects=False,
                timeout=5,
            )

    return MappedObserverEndpoint()


class ResearchStateLease:
    """One ordinary journey owner; never follow a supplied state symlink."""

    def __init__(self, directory):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ConsumerBlocked("private_owned_research_state_required")
        self.fd = os.open(directory / "journey.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(self.fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ConsumerBlocked("private_owned_research_lock_required")
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception:
            os.close(self.fd)
            raise ConsumerBlocked("research_journey_state_owner_unavailable") from None

    def close(self):
        os.close(self.fd)


class ResearchEndpoint:
    def __init__(
        self,
        origin,
        credential_file,
        *,
        ca_file=None,
        destination=None,
        basic=False,
        transport=None,
    ):
        from tessera_worker.opencode_http import OpenCodeHttpClient

        self.origin = OpenCodeHttpClient._origin(origin)
        self.credential_file = Path(credential_file)
        self.destination, self.basic = destination, basic
        verify = ssl.create_default_context(cafile=str(ca_file)) if ca_file else True
        self.client = httpx.Client(
            verify=verify, transport=transport, trust_env=False, follow_redirects=False, timeout=5
        )

    def close(self):
        self.client.close()

    def call(self, method, path, body=None):
        if not path.startswith("/") or path.startswith("//") or "#" in path:
            raise ConsumerBlocked("invalid_research_endpoint_path")
        try:
            token = private_file(self.credential_file).decode().strip()
            if not token or len(token) > 8192 or any(c.isspace() for c in token):
                raise ValueError()
            authorization = "Bearer " + token
            if self.basic:
                authorization = "Basic " + base64.b64encode(("opencode:" + token).encode()).decode()
            headers = {"Authorization": authorization}
            if self.destination:
                headers["X-Agent-Destination"] = self.destination
            with self.client.stream(
                method, self.origin + path, headers=headers, json=body
            ) as response:
                if response.status_code not in {200, 201, 202}:
                    raise ValueError()
                raw = bytearray()
                for chunk in response.iter_bytes():
                    if len(raw) + len(chunk) > 1024 * 1024:
                        raise ValueError()
                    raw.extend(chunk)
            return json.loads(raw)
        except Exception:
            raise ConsumerBlocked("research_endpoint_unavailable_or_denied") from None


class MCPStdioTools:
    """Own one real SDK stdio session; a reconnect never replays a mutation."""

    def __init__(self, config):
        self.config = config
        self.portal_manager = self.portal = self.context = self.session = self.errlog = None

    @asynccontextmanager
    async def _connect(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        environment = {
            "TESSERA_CONTROL_URL": self.config.endpoint,
            "TESSERA_AGENT_TOKEN_FILE": str(self.config.token_file),
            "TESSERA_PROJECT_ID": self.config.project_id,
            "TESSERA_AGENT_DESTINATION": self.config.destination,
        }
        if self.config.ca_file:
            environment["TESSERA_CONTROL_CA_FILE"] = str(self.config.ca_file)
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "tessera_mcp.server"], env=environment
        )
        async with stdio_client(params, errlog=self.errlog) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session

    def start(self):
        if self.session is not None:
            return
        from anyio.from_thread import start_blocking_portal

        try:
            self.errlog = open(os.devnull, "w")
            self.portal_manager = start_blocking_portal()
            self.portal = self.portal_manager.__enter__()
            self.context = self.portal.wrap_async_context_manager(self._connect())
            self.session = self.context.__enter__()
        except Exception:
            self.close()
            raise ConsumerBlocked("research_mcp_stdio_admission_failed") from None

    def call(self, tool, arguments):
        self.start()
        try:
            result = self.portal.call(self.session.call_tool, tool, arguments)
            if result.isError or not isinstance(result.structuredContent, dict):
                raise ValueError()
            return result.structuredContent
        except Exception:
            raise ConsumerBlocked("research_mcp_result_unknown") from None

    def close(self):
        try:
            if self.session is not None:
                self.context.__exit__(None, None, None)
        finally:
            self.session = None
            if self.portal_manager is not None:
                self.portal_manager.__exit__(None, None, None)
                self.portal_manager = None
            if self.errlog is not None:
                self.errlog.close()
                self.errlog = None
