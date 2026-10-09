"""Proxy-process-only resolver interface and synthetic resolver; no env/file lookup.

Python cannot guarantee whole-process memory erasure. Owned mutable buffers are
wiped deterministically; production backends must avoid copies, dumps and tracing.
"""

from dataclasses import dataclass
from typing import Literal, Protocol


class SecretBuffer:
    __slots__ = ("_buffer",)

    def __init__(self, buffer: bytearray):
        if type(buffer) is not bytearray or not buffer:
            raise ValueError("owned mutable secret buffer required")
        self._buffer = buffer

    def __repr__(self):
        return "<SecretBuffer redacted>"

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.zeroize()

    def zeroize(self):
        for i in range(len(self._buffer)):
            self._buffer[i] = 0

    def contains_in(self, value: bytes):
        return any(self._buffer) and self._buffer in value

    def __reduce__(self):
        raise TypeError("secret buffers are not serializable")


@dataclass(frozen=True)
class SecretMetadata:
    version: int
    checked_at: float
    expires_at: float
    revoked: bool = False
    purpose: Literal["salad_management"] = "salad_management"


class SecretResolver(Protocol):
    def metadata(self, reference: str) -> SecretMetadata: ...

    def resolve(self, reference: str, version: int) -> SecretBuffer:
        """Atomically recheck version/revocation/expiry and return exclusively owned bytes."""
        ...


class FakeSecretResolver:
    """Only a fixed noncredential marker; cannot import actual credentials."""

    def __init__(self, reference, *, now):
        self.reference, self.now = reference, now
        self.version, self.revoked = 1, False
        self.expires_at = now() + 60
        self.checked_at = None
        self.calls = []
        self.buffers = []

    def metadata(self, reference):
        self.calls.append("metadata")
        if reference != self.reference:
            raise ValueError("credential unavailable")
        return SecretMetadata(
            self.version,
            self.now() if self.checked_at is None else self.checked_at,
            self.expires_at,
            self.revoked,
        )

    def resolve(self, reference, version):
        self.calls.append("resolve")
        if (
            reference != self.reference
            or version != self.version
            or self.revoked
            or self.now() >= self.expires_at
        ):
            raise ValueError("credential unavailable")
        buffer = SecretBuffer(bytearray(b"synthetic-management-marker-not-a-credential"))
        self.buffers.append(buffer)
        return buffer

    def rotate(self):
        self.version += 1
        self.expires_at = self.now() + 60
        for buffer in self.buffers:
            buffer.zeroize()

    def revoke(self):
        self.revoked = True
        for buffer in self.buffers:
            buffer.zeroize()
