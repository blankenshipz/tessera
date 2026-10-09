"""Local fixture custody only. Deliberately no import/value/resolve HTTP surface."""

from .salad_secrets import SecretBuffer
from .store import Forbidden


class UnavailableSecretBackend:
    status = "unavailable"

    def materialize(self):
        raise Forbidden("Production secret enrollment is unavailable.")


class FakeAdminSecretBackend:
    status = "synthetic_only"

    def materialize(self):
        # No caller can supply bytes, environment names, paths or backend locators.
        return SecretBuffer(bytearray(b"synthetic-admin-marker-not-a-credential"))
