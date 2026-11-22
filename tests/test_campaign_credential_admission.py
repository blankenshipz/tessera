"""A stage whose provider credential is expired, revoked or mismatched is refused up front.

Submit and approval refuse with a clear reason, placement skips the candidate as
``credential_unavailable`` and records why, and a single-provider stage fails before any hold with
the reason on the campaign. Warning and unknown never refuse. Real private credential files in a
temporary directory (metadata only is read); fakes for everything else; no network, no secret.
"""

from __future__ import annotations

import time

import pytest
from salad_campaign_fixture import SaladWorld, salad_provider_reset  # noqa: F401
from test_campaign_placement_salad import _Capacity, _placed, _stage, near  # noqa: F401

from tessera_control.campaign_provider_salad import (
    CredentialBlocked,
    SaladRouteProvider,
    register_provider_instance,
)
from tessera_control.salad_secret_files import PrivateFileSecretResolver
from tessera_control.salad_secrets import SecretBuffer
from tessera_control.store import Conflict

REFERENCE = "credential-ref:salad-management"  # the reference the fixture profile pins (version 1)
LONG = 30 * 86400


def enroll(tmp_path, *, version=1, expires_at=None, clock=None, revoke=False):
    """A private credential store with the profile's reference; returns the directory."""
    path = tmp_path / "credentials"
    path.mkdir(mode=0o700, exist_ok=True)
    resolver = PrivateFileSecretResolver(path, **({"now": clock} if clock else {}))
    try:
        resolver.enroll(
            REFERENCE,
            version,
            SecretBuffer(bytearray(b"synthetic-only")),
            expires_at=time.time() + LONG if expires_at is None else expires_at,
        )
        if revoke:
            resolver.revoke(REFERENCE)
    finally:
        resolver.close()
    return path


def expired_store(tmp_path):
    return enroll(tmp_path, expires_at=101.0, clock=lambda: 100.0)


def with_store(world, directory):
    register_provider_instance(world.profiles, str(directory))


def revoke(directory):
    resolver = PrivateFileSecretResolver(directory)
    try:
        resolver.revoke(REFERENCE)
    finally:
        resolver.close()


def submit(world):
    return world.env.submit(spec=world.spec)


# -- submit and approval ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("make", "start"),
    [
        (lambda p: expired_store(p), "credential_expired: the Salad management credential"),
        (lambda p: enroll(p, revoke=True), "credential_revoked: the Salad management credential"),
        (lambda p: enroll(p, version=2), "credential_version_mismatch: profile gpu-main pins"),
    ],
)
def test_submit_refuses_an_unusable_credential_with_the_reason(tmp_path, make, start):
    world = SaladWorld(tmp_path)
    with_store(world, make(tmp_path))
    with pytest.raises(Conflict) as refused:
        submit(world)
    message = str(refused.value)
    assert message.startswith(start)
    assert REFERENCE in message and "synthetic-only" not in message
    assert "docs/salad-operator-setup.md" in message


def test_the_expired_message_names_the_version_and_the_time(tmp_path):
    world = SaladWorld(tmp_path)
    with_store(world, expired_store(tmp_path))
    with pytest.raises(CredentialBlocked) as refused:
        submit(world)
    text = str(refused.value)
    assert "version 1 expired at 1970-01-01 00:01 UTC (profile gpu-main)" in text
    assert "update the profile credential_version" in text


def test_a_credential_that_expires_after_submit_refuses_approval(tmp_path):
    world = SaladWorld(tmp_path)
    directory = enroll(tmp_path)
    with_store(world, directory)
    campaign = submit(world)
    revoke(directory)
    with pytest.raises(Conflict, match="credential_revoked"):
        world.env.approve(campaign)


def test_warning_and_unknown_never_refuse(tmp_path):
    (tmp_path / "soon").mkdir()
    soon = SaladWorld(tmp_path / "soon")
    with_store(soon, enroll(tmp_path / "soon", expires_at=time.time() + 3600))
    assert submit(soon)["status"] == "submitted"
    (tmp_path / "unknown").mkdir()
    unknown = SaladWorld(tmp_path / "unknown")  # no credential directory at all
    assert getattr(get_salad(), "credential_directory", None) is None
    assert submit(unknown)["status"] == "submitted"


def get_salad():
    from tessera_control.campaign_providers import get_provider

    return get_provider("salad")


# -- placement -------------------------------------------------------------------------------


def test_placement_skips_an_unusable_candidate_and_records_why(tmp_path, near):  # noqa: F811
    from salad_campaign_fixture import salad_spec_v2

    world = SaladWorld(tmp_path)
    near(world, 0)  # the CPU host is busy, so Salad would be chosen
    directory = enroll(tmp_path)
    with_store(world, directory)
    world.spec = salad_spec_v2(world.env, [_stage(world.env)])
    world.start()
    revoke(directory)  # the credential becomes unusable after approval
    world.tick(3)
    waiting = world.events("campaign.stage_waiting_placement")
    assert waiting, "the stage waits instead of running on the unusable candidate"
    reasons = {(r["provider_id"], r["reason"]) for r in waiting[-1]["skipped"]}
    assert ("salad", "credential_unavailable") in reasons
    assert any("credential_revoked" in r["detail"] for r in waiting[-1]["skipped"])
    assert (
        not _placed(world)
        and world.row("SELECT COUNT(*) AS n FROM campaign_stage_attempts")["n"] == 0
    )
    # The busy CPU host frees up: the stage falls through to it, never to the unusable one.
    _Capacity.slots = 1
    world.tick_until(lambda: bool(_placed(world)))
    (placed,) = _placed(world)
    assert placed["provider_id"] == "near-cpu"
    # The unusable candidate was never chosen; its skip is on the waiting event above.
    assert placed["candidates"] == ["near-cpu", "salad"]


# -- runtime ---------------------------------------------------------------------------------


def attempts(world):
    return world.row("SELECT COUNT(*) AS n FROM campaign_stage_attempts")["n"]


def test_a_single_provider_stage_fails_before_any_hold_with_the_reason(tmp_path):
    world = SaladWorld(tmp_path)
    directory = enroll(tmp_path)
    with_store(world, directory)
    world.start()
    revoke(directory)
    world.tick(3)
    campaign = world.row("SELECT * FROM campaigns WHERE id=?", world.campaign_id)
    assert "stage_failed:g1:credential_revoked" in campaign["stop_reason"]
    assert campaign["held_cents"] == 0 and attempts(world) == 0
    (blocked,) = world.events("campaign.stage_credential_blocked")
    assert blocked["stage_key"] == "g1" and blocked["state"] == "revoked"
    assert blocked["reference"] == REFERENCE and blocked["version"] == 1
    assert blocked["reason"].startswith("credential_revoked:")
    assert "synthetic-only" not in str(blocked)
    world.tick(3)
    assert len(world.events("campaign.stage_credential_blocked")) == 1  # once


@pytest.mark.parametrize(
    ("state", "code"),
    [("expired", "credential_expired"), ("version_mismatch", "credential_version_mismatch")],
)
def test_expired_and_mismatched_states_fail_the_stage_with_their_own_code(
    tmp_path, monkeypatch, state, code
):
    world = SaladWorld(tmp_path)
    world.start()
    answer = {
        "provider_id": "salad",
        "profile": "gpu-main",
        "reference": REFERENCE,
        "version": 1,
        "pinned_version": 1,
        "expires_at": 101.0,
        "state": state,
    }
    monkeypatch.setattr(SaladRouteProvider, "credential_state", lambda s, p, now=None: answer)
    world.tick(3)
    stop = world.row("SELECT stop_reason FROM campaigns WHERE id=?", world.campaign_id)
    assert f"stage_failed:g1:{code}" in stop["stop_reason"]
    assert attempts(world) == 0


def test_a_warning_is_recorded_once_and_the_stage_proceeds(tmp_path):
    world = SaladWorld(tmp_path)
    with_store(world, enroll(tmp_path, expires_at=time.time() + 3600))
    world.start()
    world.tick(6)
    (warning,) = world.events("campaign.stage_credential_warning")
    assert warning["stage_key"] == "g1" and warning["state"] == "warning"
    assert world.status() != "failed"
    assert "credential" not in (
        world.row("SELECT stop_reason FROM campaigns WHERE id=?", world.campaign_id)["stop_reason"]
        or ""
    )
    assert world.events("campaign.stage_credential_blocked") == []


def test_without_a_credential_directory_nothing_changes(tmp_path):
    world = SaladWorld(tmp_path)  # unknown state: the guardian stays the authority
    world.start()
    world.tick(6)
    assert world.events("campaign.stage_credential_blocked") == []
    assert world.events("campaign.stage_credential_warning") == []
    assert world.status() in {"running", "submitted"}
