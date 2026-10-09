"""Deterministic independent-clock simulation, not proof of an external watchdog."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from test_capability_activation import (
    HEARTBEAT_KEY,
    NOW,
    OBSERVER_KEY,
    candidate,  # noqa: F401
    signed,
)

from tessera_worker.capability_effects import BackendBlocked
from tessera_worker.capability_watchdog import (
    Heartbeat,
    LeaseWatchdog,
    TerminationEvidence,
    WatchdogJournal,
)


@pytest.fixture
def dog(candidate, tmp_path):  # noqa: F811
    journal = WatchdogJournal(tmp_path / "watchdog")
    clock = [NOW]
    kills = []
    active = [True]
    arguments = dict(
        journal=journal,
        identity="external-watchdog",
        heartbeat_key=HEARTBEAT_KEY,
        observer_key=OBSERVER_KEY,
        request_termination=lambda m, uid: kills.append(uid),
        authority=lambda m: active[0],
        now=lambda: clock[0],
    )
    watchdog = LeaseWatchdog(**arguments)
    manifest = candidate.manifest
    digest = watchdog.register(manifest, job_uid="job-uid")
    yield SimpleNamespace(**locals())
    journal.close()


def heartbeat(h, sequence=1, **changes):
    data = dict(manifest_digest=h.digest, nonce=h.manifest.nonce, sequence=sequence) | changes
    return signed(Heartbeat(**data), HEARTBEAT_KEY, b"tessera-heartbeat-v1")


def report(h, **changes):
    data = (
        dict(
            manifest_digest=h.digest,
            nonce=h.manifest.nonce,
            watchdog_identity="external-watchdog",
            job_uid="job-uid",
            observed_at=h.clock[0].isoformat(),
            job_absence_response_digest="1" * 64,
            pods_absence_response_digest="2" * 64,
            credential_revocation_digest="3" * 64,
            result="job-and-owned-pods-absent-credentials-revoked",
        )
        | changes
    )
    return signed(TerminationEvidence(**data), OBSERVER_KEY, b"tessera-termination-v1")


def test_lease_expiry_kills_after_broker_loss_and_requires_reconciliation(dog):
    h = dog
    h.clock[0] += timedelta(seconds=2)
    h.watchdog.heartbeat(heartbeat(h))
    h.clock[0] += timedelta(seconds=2)
    h.watchdog.tick()
    assert h.kills == [] and h.watchdog.status(h.digest) == "armed"
    # No broker tick/heartbeat thereafter. Only the simulated external scheduler.
    h.clock[0] += timedelta(seconds=1)
    h.watchdog.tick()
    assert h.kills == ["job-uid"] and h.watchdog.status(h.digest) == "unknown"
    h.watchdog.tick()
    assert h.kills == ["job-uid"]  # no automatic kill or effect retry
    with pytest.raises(BackendBlocked):
        h.watchdog.heartbeat(heartbeat(h, 2))
    result = h.watchdog.reconcile(report(h))
    assert result["state"] == "terminated"
    assert h.watchdog.status(h.digest) == "terminated"
    assert h.watchdog.events(h.digest)[-1]["evidence_digest"] == result["evidence_digest"]
    with pytest.raises(BackendBlocked):
        h.watchdog.register(h.manifest, job_uid="replacement")


@pytest.mark.parametrize("cause", ["revoked", "lease-expired", "clock-rollback", "explicit-revoke"])
def test_watchdog_fences_authority_expiry_and_revocation(dog, cause):
    h = dog
    if cause == "revoked":
        h.active[0] = False
    elif cause == "lease-expired":
        h.clock[0] += timedelta(seconds=3)
    elif cause == "clock-rollback":
        h.clock[0] -= timedelta(seconds=1)
    else:
        h.watchdog.revoke(h.digest)
    h.watchdog.tick()
    assert h.kills == ["job-uid"] and h.watchdog.status(h.digest) == "unknown"


@pytest.mark.parametrize("sequence", [1, 3])
def test_heartbeat_replay_or_gap_stops_lease(dog, sequence):
    h = dog
    h.watchdog.heartbeat(heartbeat(h))
    with pytest.raises(BackendBlocked):
        h.watchdog.heartbeat(heartbeat(h, sequence))
    assert h.watchdog.status(h.digest) == "unknown" and h.kills == ["job-uid"]


def test_no_heartbeat_can_extend_absolute_expiry(dog):
    h = dog
    for seq in range(1, 5):
        h.clock[0] += timedelta(seconds=2)
        h.watchdog.heartbeat(heartbeat(h, seq))
    h.clock[0] += timedelta(seconds=2)
    with pytest.raises(BackendBlocked):
        h.watchdog.heartbeat(heartbeat(h, 5))
    assert h.kills == ["job-uid"]


def test_delayed_authority_reply_cannot_extend_expired_lease(dog):
    h = dog

    def delayed(_):
        h.clock[0] += timedelta(seconds=3)
        return True

    h.watchdog.authority = delayed
    with pytest.raises(BackendBlocked):
        h.watchdog.heartbeat(heartbeat(h))
    assert h.kills == ["job-uid"]


def test_lost_kill_reply_does_not_confirm_or_leak(dog):
    h = dog

    def lost(*_):
        raise RuntimeError("secret-canary-from-provider")

    h.watchdog.request_termination = lost
    h.watchdog.revoke(h.digest)
    assert h.watchdog.status(h.digest) == "unknown"
    assert "secret-canary" not in str(h.watchdog.events(h.digest))
    h.watchdog.reconcile(report(h))
    assert h.watchdog.status(h.digest) == "terminated"


@pytest.mark.parametrize(
    "field,value",
    [
        ("nonce", "0" * 64),
        ("manifest_digest", "0" * 64),
        ("job_uid", "other"),
        ("watchdog_identity", "broker"),
        ("job_absence_response_digest", "0" * 64),
        ("pods_absence_response_digest", "0" * 64),
        ("credential_revocation_digest", "0" * 64),
        ("observed_at", (NOW - timedelta(seconds=6)).isoformat()),
        ("observed_at", (NOW + timedelta(seconds=1)).isoformat()),
    ],
)
def test_termination_evidence_mismatch(dog, field, value):
    h = dog
    h.watchdog.revoke(h.digest)
    with pytest.raises(BackendBlocked):
        h.watchdog.reconcile(report(h, **{field: value}))
    assert h.watchdog.status(h.digest) == "unknown"


def test_prior_termination_report_cannot_confirm_new_kill(dog):
    h = dog
    old = report(h)
    h.clock[0] += timedelta(seconds=1)
    h.watchdog.revoke(h.digest)
    with pytest.raises(BackendBlocked):
        h.watchdog.reconcile(old)


def test_restart_fences_live_lease_without_resuming(dog):
    h = dog
    h.journal.close()
    reopened = WatchdogJournal(h.journal.root)
    try:
        watchdog = LeaseWatchdog(**(h.arguments | {"journal": reopened}))
        assert watchdog.status(h.digest) == "terminate_requested"
        watchdog.tick()
        assert watchdog.status(h.digest) == "unknown" and h.kills == ["job-uid"]
        with pytest.raises(BackendBlocked):
            watchdog.register(h.manifest, job_uid="new")
    finally:
        reopened.close()


def test_kill_ack_is_not_termination_proof(dog):
    dog.watchdog.request_termination = lambda *_: True
    dog.watchdog.revoke(dog.digest)
    assert dog.watchdog.status(dog.digest) == "unknown"


def test_unsigned_heartbeat_and_report_rejected(dog):
    bad = signed(Heartbeat(manifest_digest=dog.digest, nonce=dog.manifest.nonce, sequence=1))
    with pytest.raises(BackendBlocked):
        dog.watchdog.heartbeat(bad)
    assert dog.watchdog.status(dog.digest) == "armed"
