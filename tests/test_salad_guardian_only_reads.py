"""q-2716 review F5: the provider's event and log streams are guardian-only reads.

A worker's ``ProxyRequest`` under an observe effect authorizes exact group/instance reads only.
``read_events`` and ``read_logs`` are sent by the guardian through its own transport and its own
claim (the exit watch's throttled read, the one durable-claimed container-log read), so the
worker-facing paths refuse them before any claim: no effect nonce is spent and no provider call
is made."""

from __future__ import annotations

import pytest
from test_salad_proxy import fixture, request
from test_remote_execution_routing import dispatch
from test_salad_running_latch import EFFECT, _Store, fake_server

from tessera_control.salad_proxy import GUARDIAN_ONLY_OPERATIONS
from tessera_control.salad_proxy_contract import ProxyRequest


def test_the_guardian_only_set_is_exactly_the_event_and_log_streams():
    assert GUARDIAN_ONLY_OPERATIONS == {"read_events", "read_logs"}


class _CountingStore(_Store):
    def __init__(self):
        super().__init__()
        self.claims = 0

    def claim_proxy(self, *args, **kwargs):
        self.claims += 1
        return super().claim_proxy(*args, **kwargs)


class _CountingTransport:
    def __init__(self):
        self.calls = []

    def send(self, operation, selection, provider_id, secret):
        self.calls.append(operation)
        raise RuntimeError("no provider in this test")


@pytest.mark.parametrize("operation", sorted(GUARDIAN_ONLY_OPERATIONS))
def test_the_trusted_server_refuses_a_worker_stream_read_before_any_claim(operation):
    server = fake_server()
    server.store, server.transport = _CountingStore(), _CountingTransport()
    result = server.request(
        ProxyRequest(route_id="exact-route", effect_id=EFFECT, operation=operation),
        preflight=None,
    )
    assert (result.status, result.reason) == ("blocked", "admission_denied")
    assert server.store.claims == 0 and server.transport.calls == []
    # The same observe effect still sends an exact read (control: the refusal is the operation).
    server.request(
        ProxyRequest(route_id="exact-route", effect_id=EFFECT, operation="read_group"),
        preflight=None,
    )
    assert server.store.claims >= 1 and server.transport.calls == ["read_group"]


@pytest.mark.parametrize("operation", sorted(GUARDIAN_ONLY_OPERATIONS))
def test_the_proxy_refuses_a_worker_stream_read_and_keeps_the_effect_nonce(tmp_path, operation):
    h = fixture(tmp_path)
    dispatch(h, "create")
    req, preflight = request(h, operation, action="observe")
    calls = len(h.transport.calls)
    assert h.proxy.request_fake(req, preflight=preflight).reason == "admission_denied"
    assert len(h.transport.calls) == calls
    # The observe effect was not consumed: an exact read on it still goes through.
    exact = ProxyRequest(route_id=req.route_id, effect_id=req.effect_id, operation="read_group")
    assert h.proxy.request_fake(exact, preflight=preflight).status == "observed"


@pytest.mark.parametrize("operation", sorted(GUARDIAN_ONLY_OPERATIONS))
def test_a_refused_worker_stream_read_confirms_no_termination(tmp_path, operation):
    h = fixture(tmp_path)
    dispatch(h, "create")
    req, preflight = request(h, operation, action="observe")
    result = h.proxy.request_fake(req, preflight=preflight)
    assert result.status == "blocked" and result.observation is None
    assert all(call.operation != operation for call in h.transport.calls)
