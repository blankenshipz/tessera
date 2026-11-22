"""Credential expiry health: the evaluator, the Salad provider's metadata-only reads, the route.

No network and no real credential: secrets are synthetic markers, and a test asserts they never
leave the credential file (not in an entry, a route answer, an exception or captured output).
"""

from __future__ import annotations

import json
import os
import time

import pytest
from test_provider_registration_api import DESTINATION, ROUTE, World, profile  # noqa: F401

from tessera_control import credential_health as health
from tessera_control.campaign_provider_salad import SaladRouteProvider, register_provider_instance
from tessera_control.salad_campaign_profiles import SaladCampaignProfiles
from tessera_control.salad_secret_files import PrivateFileSecretResolver
from tessera_control.salad_secrets import SecretBuffer

SECRET = b"synthetic-credential-marker-ZXCV-0123456789"
REFERENCE = "credential-ref:salad-management"
NOW = 1_800_000_000.0
DAY = 86400


def meta(**over):
    value = {"version": 1, "expires_at": NOW + 10 * DAY, "revoked": False}
    value.update(over)
    return value


# -- evaluate ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("metadata", "pinned", "state"),
    [
        (meta(), 1, "ok"),
        (meta(expires_at=NOW + DAY + 1), 1, "ok"),  # just outside the window
        (meta(expires_at=NOW + DAY), 1, "warning"),  # on the boundary
        (meta(expires_at=NOW + 60), 1, "warning"),
        (meta(expires_at=NOW), 1, "expired"),
        (meta(expires_at=NOW - 5), 1, "expired"),
        (meta(revoked=True), 1, "revoked"),
        (meta(revoked=True, expires_at=NOW - 5), 1, "revoked"),  # revoked wins
        (meta(version=2), 1, "version_mismatch"),
        (meta(version=2, expires_at=NOW - 1), 1, "expired"),  # a dead credential is expired first
        (meta(version=2), None, "ok"),  # nothing pinned, nothing to mismatch
        (None, 1, "unknown"),
        ({"version": 1}, 1, "unknown"),  # no expiry recorded
    ],
)
def test_evaluate_states(metadata, pinned, state):
    got = health.evaluate(metadata, NOW, pinned_version=pinned)
    assert got["state"] == state
    assert set(got) == {
        "state",
        "seconds_remaining",
        "expires_at",
        "revoked",
        "version",
        "pinned_version",
        "reason",
    }


def test_only_dead_or_mismatched_credentials_block():
    assert health.BLOCKING_STATES == {"expired", "revoked", "version_mismatch"}
    assert "unknown" not in health.BLOCKING_STATES and "warning" not in health.BLOCKING_STATES


def test_summarize_reports_the_worst_state_and_counts():
    items = [{"state": "ok"}, {"state": "warning"}, {"state": "expired"}, {"state": "unknown"}]
    summary = health.summarize(items)
    assert summary["worst_state"] == "expired"
    assert summary["warning_count"] == 1 and summary["expired_count"] == 1
    assert health.summarize([])["worst_state"] == "ok"


# -- the provider reads metadata only ----------------------------------------------------------


def store(tmp_path, *, expires_in=10 * DAY, version=1, revoke=False):
    directory = tmp_path / "creds"
    directory.mkdir(mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    # Enrolled "in the past" so an expiry before the real now can be written.
    resolver = PrivateFileSecretResolver(directory, now=lambda: time.time() - 40 * DAY)
    resolver.enroll(
        REFERENCE, version, SecretBuffer(bytearray(SECRET)), expires_at=time.time() + expires_in
    )
    if revoke:
        resolver.revoke(REFERENCE)
    resolver.close()
    return directory


def salad_profile(env, **over):
    return SaladCampaignProfiles.model_validate(
        {"schema": "tessera.salad.campaign-profiles.v1", "profiles": [profile(env, **over)]}
    )


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


@pytest.fixture(autouse=True)
def reset_salad():
    yield
    register_provider_instance(None)


def test_read_metadata_returns_the_header_only(tmp_path):
    directory = store(tmp_path)
    resolver = PrivateFileSecretResolver(directory)
    try:
        got = resolver.read_metadata(REFERENCE)
    finally:
        resolver.close()
    assert set(got) == {"version", "expires_at", "revoked", "purpose"}
    assert SECRET.decode() not in json.dumps(got)


def test_read_metadata_never_reads_the_secret_bytes(tmp_path, monkeypatch):
    directory = store(tmp_path)
    resolver = PrivateFileSecretResolver(directory)
    reads = []
    real = os.fdopen

    def spy(fd, mode="r", *args, **kwargs):
        stream = real(fd, mode, *args, **kwargs)
        original = stream.readline

        def readline(limit=-1):
            line = original(limit)
            reads.append(line)
            return line

        class Wrapped:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                stream.close()

            def readline(self, limit=-1):
                return readline(limit)

            def read(self, *args):  # pragma: no cover - the test fails if it is reached
                raise AssertionError("the secret bytes must not be read")

            def readinto(self, *args):  # pragma: no cover
                raise AssertionError("the secret bytes must not be read")

        return Wrapped()

    monkeypatch.setattr(os, "fdopen", spy)
    try:
        resolver.read_metadata(REFERENCE)
    finally:
        monkeypatch.undo()
        resolver.close()
    assert len(reads) == 1 and SECRET not in b"".join(reads)


@pytest.mark.parametrize(
    ("kwargs", "state"),
    [
        ({}, "ok"),
        ({"expires_in": 3600}, "warning"),
        ({"expires_in": -3600}, "expired"),
        ({"revoke": True}, "revoked"),
        ({"version": 2}, "version_mismatch"),  # the profile pins version 1
    ],
)
def test_provider_credential_health_states(tmp_path, world, kwargs, state):
    directory = store(tmp_path, **kwargs)
    provider = SaladRouteProvider(salad_profile(world.env), str(directory))
    (entry,) = provider.credential_health()
    assert entry["state"] == state
    assert entry["provider_id"] == "salad" and entry["profile"] == "cpu-main"
    assert entry["reference"] == REFERENCE
    assert SECRET.decode() not in json.dumps(entry)


def test_without_a_directory_or_with_an_unreadable_one_the_state_is_unknown(tmp_path, world):
    profiles = salad_profile(world.env)
    (entry,) = SaladRouteProvider(profiles).credential_health()
    assert entry["state"] == "unknown" and "no credential directory" in entry["reason"]
    missing = SaladRouteProvider(profiles, str(tmp_path / "nope"))
    assert missing.credential_health()[0]["state"] == "unknown"
    empty = tmp_path / "empty"
    empty.mkdir(mode=0o700)
    assert SaladRouteProvider(profiles, str(empty)).credential_health()[0]["state"] == "unknown"
    assert SaladRouteProvider(None).credential_health() == []


def test_a_wrong_mode_store_is_unknown_not_a_crash(tmp_path, world):
    directory = store(tmp_path)
    os.chmod(directory, 0o755)
    entry = SaladRouteProvider(salad_profile(world.env), str(directory)).credential_health()[0]
    assert entry["state"] == "unknown"


def test_collect_gathers_registered_providers_and_survives_a_failing_one(tmp_path, world):
    from tessera_control import campaign_providers as providers

    directory = store(tmp_path, expires_in=-60)
    register_provider_instance(salad_profile(world.env), str(directory))
    assert [e["state"] for e in health.collect()] == ["expired"]

    class Broken:
        credential_health = staticmethod(lambda now: 1 / 0)
        descriptor = type("D", (), {"provider_id": "broken-test"})()

    providers._PROVIDERS["broken-test"] = Broken()
    try:
        states = {e["provider_id"]: e["state"] for e in health.collect()}
    finally:
        providers._PROVIDERS.pop("broken-test", None)
    assert states == {"salad": "expired", "broken-test": "unknown"}


# -- the route and the operator health ---------------------------------------------------------


def test_the_route_serves_a_human_and_a_scoped_agent_but_not_an_unscoped_one_or_a_worker(
    tmp_path, world
):
    directory = store(tmp_path, expires_in=3600)
    register_provider_instance(salad_profile(world.env), str(directory))
    human = world.client.get("/credential-health", headers=world.human())
    assert human.status_code == 200
    body = human.json()
    assert body["worst_state"] == "warning" and body["warning_count"] == 1
    assert body["items"][0]["reference"] == REFERENCE
    assert SECRET.decode() not in human.text
    scoped = world.client.get("/credential-health", headers=world.agent(["provider_trust:read"]))
    assert scoped.status_code == 200
    unscoped = world.client.get("/credential-health", headers=world.agent(["campaign:read"]))
    assert unscoped.status_code in {401, 403}
    worker = {"Authorization": "Bearer worker-token", "X-Agent-Destination": DESTINATION}
    assert world.client.get("/credential-health", headers=worker).status_code in {401, 403}


def test_the_operator_health_carries_the_credential_summary_and_an_alert(tmp_path, world):
    directory = store(tmp_path, expires_in=-60)
    register_provider_instance(salad_profile(world.env), str(directory))
    response = world.client.get("/operator/health", headers=world.root())
    assert response.status_code == 200
    body = response.json()
    assert body["credentials"]["worst_state"] == "expired"
    assert {"code": "credential_expired", "severity": "critical"} in body["alerts"]
    assert SECRET.decode() not in response.text


def test_a_warning_alert_when_expiry_is_near(tmp_path, world):
    directory = store(tmp_path, expires_in=3600)
    register_provider_instance(salad_profile(world.env), str(directory))
    body = world.client.get("/operator/health", headers=world.root()).json()
    assert {"code": "credential_expiring", "severity": "warning"} in body["alerts"]


# -- the serve flag and the providers file -----------------------------------------------------


def test_the_providers_file_may_name_the_directory_and_it_must_be_absolute(tmp_path, world):
    from tessera_control.campaign_provider_salad import register_from_config

    directory = store(tmp_path)
    register_from_config(
        {
            "salad_profiles": salad_profile(world.env).model_dump(mode="json", by_alias=True),
            "salad_credential_directory": str(directory),
        }
    )
    assert health.collect()[0]["state"] == "ok"
    with pytest.raises(ValueError, match="absolute"):
        register_from_config({"salad_credential_directory": "relative/dir"})


def test_serve_refuses_the_directory_given_twice(tmp_path, monkeypatch, capsys):
    from tessera_control import cli

    providers = tmp_path / "providers.json"
    providers.write_text(
        json.dumps(
            {
                "schema": "tessera.campaign-providers.v1",
                "salad_credential_directory": str(tmp_path),
            }
        )
    )
    providers.chmod(0o600)
    monkeypatch.setattr(
        "sys.argv",
        [
            "tessera",
            "serve",
            "--state-dir",
            str(tmp_path / "state"),
            "--port",
            "18765",
            "--campaign-providers",
            str(providers),
            "--salad-credential-directory",
            str(tmp_path),
        ],
    )
    assert cli.main() == 2
    assert "not both" in capsys.readouterr().err
