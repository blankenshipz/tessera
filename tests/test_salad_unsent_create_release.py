"""A ready-admitted create the trusted server never sent is reported unsent and released fast.

After the guardian admitted a create (``ready``) the trusted server still rechecks the claim and
the credential before the one provider call. A failure there provably sent no create, so the
server reports it through the existing ``create_rejected`` path with the transport's own
``request_not_sent`` summary. The guardian fences at once and releases the reservation only on
its own exact-name 404 and complete project-list reads after CREATE_REJECTED_SETTLE_SECONDS,
instead of holding the open-frame slot until the route deadline plus the lost-create margin. A
failure once ``transport.send`` was called is never reported unsent. Fake provider only."""

from __future__ import annotations

import sqlite3

import pytest

pytest.importorskip("cryptography")
from cryptography.hazmat.primitives.asymmetric import ec
from test_remote_execution_routing import dispatch
from test_salad_safety_service import (
    Provider,
    _absence_reads,
    _kinds,
    _next_reservation,
    make_service,
    reservation,
)
from test_salad_server import fixture, preflight, wire

from tessera_control.salad_http import NOT_SENT_SUMMARY
from tessera_control.salad_safety_service import (
    CREATE_REJECTED_SETTLE_SECONDS,
    LOST_CREATE_MARGIN_SECONDS,
)


def _recording_guardian(h, monkeypatch, on_ready=None):
    """Record every guardian body; ``on_ready`` runs right after ``ready`` was admitted."""
    sent = []
    exchange = h.server.safety._exchange

    def service(body):
        exchange(body)
        sent.append(body)
        if body["operation"] == "ready" and on_ready is not None:
            on_ready()

    def call(body, cap=16384):
        sent.append(body)
        return dict(status="reserved")

    monkeypatch.setattr(h.server.safety, "_exchange", service)
    monkeypatch.setattr(h.server.safety, "_call", call)
    return sent


def _no_create_post(factory):
    return not any(m == "POST" and p.endswith("/containers") for m, p, _, _ in factory.calls)


def _fence_capability(h):
    with sqlite3.connect(h.store.database_path) as db:
        db.execute("UPDATE capability_policies SET status='revoked'")


def _server_unsent_report(tmp_path, monkeypatch):
    """The real trusted server: the second (post-ready) use-time recheck refuses the claim."""
    tmp_path.mkdir(mode=0o700)
    h = fixture(tmp_path, monkeypatch)
    sent = _recording_guardian(h, monkeypatch, on_ready=lambda: _fence_capability(h))
    factory = wire(h, *preflight(h, absent=True))
    state = dispatch(h, "create")
    assert state["receipts"][-1]["proxy_reason"] == "admission_denied"
    assert _no_create_post(factory)
    operations = [b["operation"] for b in sent]
    assert operations == ["reserve", "ready", "create_rejected"]
    report = sent[-1]
    assert report["rejection"] == NOT_SENT_SUMMARY
    assert report["reservation"] == sent[1]["reservation"]
    return report["rejection"]


def _guardian_after_ready(tmp_path):
    """The guardian holding a ready-admitted reservation (ready at 100), group never created."""
    tmp_path.chmod(0o700)
    clock = [100.0]
    provider = Provider()
    provider.group_missing = True
    s = make_service(tmp_path, lambda: clock[0], ec.generate_private_key(ec.SECP256R1()), provider)
    s.resolver.expires_at = 100 + 86400
    b = reservation()
    s.loop()
    s.admin(b)
    s.admin(dict(operation="ready", reservation=b["reservation"]))
    assert s._row(b["reservation"])["last_ready_at"] == 100.0
    return s, b, clock, provider


def test_post_ready_recheck_failure_is_reported_unsent_and_frees_the_slot(tmp_path, monkeypatch):
    rejection = _server_unsent_report(tmp_path / "server", monkeypatch)
    (tmp_path / "guardian").mkdir(mode=0o700)
    s, b, clock, provider = _guardian_after_ready(tmp_path / "guardian")
    try:
        s.admin(
            dict(operation="create_rejected", reservation=b["reservation"], rejection=rejection)
        )
        row = s._row(b["reservation"])
        assert row["recovery"] == 1 and row["provider_id"] is None
        # Fenced: no further ready; the slot stays held through the settle.
        with pytest.raises(ValueError):
            s.admin(dict(operation="ready", reservation=b["reservation"]))
        clock[0] = 100 + CREATE_REJECTED_SETTLE_SECONDS - 1
        s.loop()
        assert _absence_reads(provider) == 0
        with pytest.raises(ValueError, match="single_operation_orphan_exposure"):
            s.admin(_next_reservation(s, clock))
        # After the settle (long before deadline + margin) the guardian's own reads release it.
        clock[0] = 100 + CREATE_REJECTED_SETTLE_SECONDS
        assert clock[0] < b["deadline"] + LOST_CREATE_MARGIN_SECONDS
        s.loop()
        assert _absence_reads(provider) == 1
        status = s._status(s._row(b["reservation"]))
        assert status["status"] == "reconciled" and status["reason"] == "create_rejected_absent"
        record = status["lost_create"]
        assert record["name_read"] == "not_found" and record["list_read"] == "absent"
        assert record["margin_seconds"] == CREATE_REJECTED_SETTLE_SECONDS
        assert record["rejection"]["status"] == 0
        # The open-frame slot is free for another operation's reserve.
        clock[0] += 1
        s.admin(_next_reservation(s, clock))
    finally:
        s.close()


@pytest.mark.parametrize(
    "absence,list_read",
    [
        ("unknown", "absent"),
        ("raise", "absent"),
        ("present", "absent"),
        ("not_found", "unavailable"),
        ("not_found", "absent_partial"),
        ("not_found", "present"),
    ],
)
def test_unsent_report_never_releases_without_a_confirmed_absence(
    tmp_path, monkeypatch, absence, list_read
):
    rejection = _server_unsent_report(tmp_path / "server", monkeypatch)
    (tmp_path / "guardian").mkdir(mode=0o700)
    s, b, clock, provider = _guardian_after_ready(tmp_path / "guardian")
    provider.absence, provider.list_read = absence, list_read
    try:
        s.admin(
            dict(operation="create_rejected", reservation=b["reservation"], rejection=rejection)
        )
        clock[0] = 100 + CREATE_REJECTED_SETTLE_SECONDS + 1
        s.loop()
        assert _absence_reads(provider) == 1
        assert s._status(s._row(b["reservation"]))["status"] == "reserved"
        assert s._row(b["reservation"])["lost_create_resolution"] is None
        with pytest.raises(ValueError, match="single_operation_orphan_exposure"):
            s.admin(_next_reservation(s, clock))
        assert _kinds(s) == ["create_rejection_reported"]
    finally:
        s.close()


def test_post_ready_credential_failure_is_reported_unsent(tmp_path, monkeypatch):
    h = fixture(tmp_path, monkeypatch)
    management = h.server.policy.management
    sent = _recording_guardian(
        h, monkeypatch, on_ready=lambda: h.server.resolver.revoke(management.reference)
    )
    factory = wire(h, *preflight(h, absent=True))
    state = dispatch(h, "create")
    assert state["receipts"][-1]["proxy_reason"] == "credential_unavailable"
    assert _no_create_post(factory)
    assert [b["operation"] for b in sent] == ["reserve", "ready", "create_rejected"]
    assert sent[-1]["rejection"] == NOT_SENT_SUMMARY


def test_a_failed_unsent_report_keeps_the_original_result(tmp_path, monkeypatch):
    h = fixture(tmp_path, monkeypatch)
    _recording_guardian(h, monkeypatch, on_ready=lambda: _fence_capability(h))

    def unavailable(body, cap=16384):
        raise ValueError("independent service unavailable")

    monkeypatch.setattr(h.server.safety, "_call", unavailable)
    wire(h, *preflight(h, absent=True))
    state = dispatch(h, "create")
    assert state["receipts"][-1]["proxy_reason"] == "admission_denied"


def test_a_failure_once_send_was_called_is_not_reported_unsent(tmp_path, monkeypatch):
    h = fixture(tmp_path, monkeypatch)
    sent = _recording_guardian(h, monkeypatch)
    wire(h, *preflight(h, absent=True))
    calls = []

    def send_then_fail(*args, **kwargs):
        calls.append(args[0])
        raise RuntimeError("connection reset after the request was written")

    monkeypatch.setattr(h.server.transport, "send", send_then_fail)
    state = dispatch(h, "create")
    assert calls == ["create_stopped"]
    assert state["receipts"][-1]["proxy_reason"] == "provider_unavailable"
    assert "create_rejected" not in [b["operation"] for b in sent]


def test_a_failed_ready_is_not_reported_unsent(tmp_path, monkeypatch):
    """Before a successful ``ready`` the guardian admitted no create; the never-ready path owns
    that reservation, so nothing is reported."""
    h = fixture(tmp_path, monkeypatch)
    sent = []
    exchange = h.server.safety._exchange

    def service(body):
        sent.append(body)
        if body["operation"] == "ready":
            raise ValueError("not retained")
        exchange(body)

    monkeypatch.setattr(h.server.safety, "_exchange", service)
    monkeypatch.setattr(h.server.safety, "_call", lambda *a, **k: pytest.fail("reported"))
    wire(h, *preflight(h, absent=True))
    state = dispatch(h, "create")
    assert state["receipts"][-1]["proxy_reason"] == "provider_unavailable"
    assert [b["operation"] for b in sent] == ["reserve", "ready"]
