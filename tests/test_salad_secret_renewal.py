import os

import pytest

from tessera_control.salad_secret_files import PrivateFileSecretResolver
from tessera_control.salad_secrets import SecretBuffer


@pytest.fixture
def backend(tmp_path):
    path = tmp_path / "credentials"
    path.mkdir(mode=0o700)
    clock = [100.0]
    resolver = PrivateFileSecretResolver(path, now=lambda: clock[0])
    resolver.enroll(
        "credential-ref:fixture", 1, SecretBuffer(bytearray(b"synthetic-only")), expires_at=101
    )
    clock[0] = 102.0
    yield resolver, path
    resolver.close()


def test_expired_same_key_renewal_pins_version_and_preserves_private_custody(backend):
    resolver, path = backend
    with pytest.raises(ValueError):
        resolver.resolve("credential-ref:fixture", 1)
    metadata = resolver.renew("credential-ref:fixture", expected_version=1, expires_at=86502)
    assert (metadata.version, metadata.expires_at, metadata.purpose, metadata.revoked) == (
        2,
        86502,
        "salad_management",
        False,
    )
    with pytest.raises(ValueError):
        resolver.resolve("credential-ref:fixture", 1)
    with resolver.resolve("credential-ref:fixture", 2) as secret:
        assert secret._buffer == bytearray(b"synthetic-only")
    assert not any(secret._buffer)
    assert os.stat(path / "fixture.credential").st_mode & 0o777 == 0o600
    with pytest.raises(ValueError):
        resolver.renew("credential-ref:fixture", expected_version=1, expires_at=86502)
    assert resolver.metadata("credential-ref:fixture").version == 2


@pytest.mark.parametrize(
    "version,expiry",
    [
        (True, 200),
        (0, 200),
        (2, 200),
        (1, True),
        (1, 102),
        (1, 86503),
        (1, float("nan")),
        (1, float("inf")),
    ],
)
def test_invalid_or_stale_renewal_leaves_record_unchanged(backend, version, expiry):
    resolver, path = backend
    before = (path / "fixture.credential").read_bytes()
    with pytest.raises(ValueError):
        resolver.renew("credential-ref:fixture", expected_version=version, expires_at=expiry)
    assert (path / "fixture.credential").read_bytes() == before


def test_revocation_and_filesystem_guards_still_apply(backend):
    resolver, path = backend
    resolver.revoke("credential-ref:fixture")
    with pytest.raises(ValueError):
        resolver.renew("credential-ref:fixture", expected_version=1, expires_at=200)
    assert resolver.metadata("credential-ref:fixture").revoked
    os.chmod(path, 0o755)
    with pytest.raises(ValueError):
        resolver.renew("credential-ref:fixture", expected_version=1, expires_at=200)
    os.chmod(path, 0o700)


def test_failed_atomic_write_zeroizes_owned_secret_and_keeps_record(backend, monkeypatch):
    resolver, path = backend
    before = (path / "fixture.credential").read_bytes()
    retained = []

    def fail_write(directory, name, metadata, secret):
        retained.append(secret)
        raise OSError("synthetic write failure")

    monkeypatch.setattr(resolver, "_write", fail_write)
    with pytest.raises(OSError):
        resolver.renew("credential-ref:fixture", expected_version=1, expires_at=200)
    assert not any(retained[0]._buffer)
    assert (path / "fixture.credential").read_bytes() == before
