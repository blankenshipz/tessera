"""Transport checks with synthetic temp-backend credentials; never real runtime auth."""

import socket
import threading
import time

import pytest
import uvicorn
from test_generated_outputs import output_setup  # noqa: F401

from tessera_agent.research_consumer_store import ConsumerBlocked
from tessera_agent.research_transports import MCPStdioTools, ResearchEndpoint, ResearchStateLease
from tessera_mcp.config import Config


def test_private_state_rejects_shared_owner_and_concurrent_journey(tmp_path):
    directory = tmp_path / "private"
    lease = ResearchStateLease(directory)
    try:
        with pytest.raises(ConsumerBlocked, match="owner_unavailable"):
            ResearchStateLease(directory)
    finally:
        lease.close()
    directory.chmod(0o755)
    with pytest.raises(ConsumerBlocked, match="private_owned"):
        ResearchStateLease(directory)


def test_real_mcp_sdk_stdio_reads_actual_temp_http_backend_identity(request, tmp_path):
    _, http, _, project, sender, *_ = request.getfixturevalue("output_setup")
    # This freshly minted isolated test-backend token is synthetic test state,
    # not a read of any installed credential, OAuth/account store or operator file.
    token_file = tmp_path / "synthetic-test-token"
    token_file.write_text(sender["Authorization"].removeprefix("Bearer "))
    token_file.chmod(0o600)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(http.app, log_level="critical", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    tools = MCPStdioTools(
        Config(f"http://127.0.0.1:{port}", token_file, project, sender["X-Agent-Destination"])
    )
    endpoint = ResearchEndpoint(
        f"http://127.0.0.1:{port}", token_file, destination=sender["X-Agent-Destination"]
    )
    try:
        deadline = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        status = endpoint.call("GET", "/external-agent/status")
        assert status["project_id"] == project
        result = tools.call("tessera_status", {})
        assert result["project_id"] == project
        assert result["destination"] == sender["X-Agent-Destination"]
    finally:
        tools.close()
        endpoint.close()
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
    assert not thread.is_alive()


def test_collector_and_peer_cannot_disable_verified_tls():
    from tessera_agent.provenance_client import ProvenanceClient
    from tessera_agent.research_peer import ResearchPeer

    with pytest.raises(ValueError, match="cannot be disabled"):
        ProvenanceClient("https://control.invalid", "synthetic", "openai:personal", verify=False)
    with pytest.raises(ConsumerBlocked, match="tls_verification_required"):
        ResearchPeer("https://peer.invalid", "synthetic", verify=False)
