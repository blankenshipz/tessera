"""Synthetic transport checks; forbidden content never crosses peer boundary."""

import httpx
import pytest

from tessera_agent.research_consumer_store import ConsumerBlocked
from tessera_agent.research_peer import ResearchPeer, validate_generated


def test_peer_extra_raw_content_is_rejected_before_transport():
    calls = []
    peer = ResearchPeer(
        "http://127.0.0.1:8771",
        "synthetic-token",
        transport=httpx.MockTransport(lambda request: calls.append(request)),
    )
    with pytest.raises(ConsumerBlocked, match="peer_reference_envelope_invalid"):
        peer.send(
            "result",
            {
                "reference": "output",
                "authority": {},
                "generated": {},
                "content": "synthetic-private-output",
            },
            "stable-message",
        )
    assert not calls
    peer.close()


def test_consumer_requires_research_not_worker_manifest_family():
    with pytest.raises(ConsumerBlocked, match="research_generated_family_required"):
        validate_generated({"artifact_id": "worker-artifact"})
