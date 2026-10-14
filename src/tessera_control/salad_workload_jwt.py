"""Strict Salad IMDS ES256 verification, with a fixed bounded public JWKS origin.

Issuer comes from Salad's published JWT decoded example (jwt_2.png). Project
authorization is provider-API-derived: the documented token has no project claim.
No unverified claim or token header may select a URL, key, tenant or operation.
"""

import base64
import http.client
import json
import re
import time
import uuid

ISSUER = "https://matrix-api.salad.io"
JWKS_HOST = "matrix-rest-api.salad.com"
JWKS_PATH = "/.well-known/workload-jwks.json"


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("invalid_json")
            result[key] = value
        return result

    return json.loads(
        raw,
        object_pairs_hook=pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid_json")),
    )


def decode64(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("invalid_encoding")
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)


def normalized_uuid(value):
    if not isinstance(value, str):
        raise ValueError("invalid_identity")
    return uuid.UUID(value).hex


class SaladWorkloadJWT:
    """One fixed JWKS cache; unknown kids do not cause attacker-driven fetches."""

    def __init__(self, *, clock=time.time, connection_factory=http.client.HTTPSConnection):
        self.clock = clock
        self.factory = connection_factory
        self.keys = {}
        self.refreshed = None
        self.last_attempt = None

    def refresh(self):
        now = self.clock()
        if self.last_attempt is not None and now - self.last_attempt < 60:
            return
        self.last_attempt = now
        connection = self.factory(JWKS_HOST, timeout=2)
        try:
            connection.request("GET", JWKS_PATH, headers={"Accept": "application/json"})
            response = connection.getresponse()
            if response.status != 200:
                raise ValueError("jwks_unavailable")
            raw = response.read(16385)
            if len(raw) > 16384 or self.clock() - now > 2:
                raise ValueError("jwks_unavailable")
            self.load(strict_json(raw), observed_at=now)
        except Exception:
            raise ValueError("jwks_unavailable") from None
        finally:
            connection.close()

    def load(self, document, *, observed_at):
        """Public-key loader; production supplies only fixed-origin HTTPS bytes."""
        from cryptography.hazmat.primitives.asymmetric import ec

        if type(document) is not dict or set(document) != {"keys"}:
            raise ValueError("invalid_jwks")
        if type(document["keys"]) is not list or not 1 <= len(document["keys"]) <= 4:
            raise ValueError("invalid_jwks")
        keys = {}
        for key in document["keys"]:
            if (
                type(key) is not dict
                or not {"kid", "kty", "crv", "x", "y"} <= set(key)
                or set(key) - {"kid", "kty", "crv", "x", "y", "alg", "use", "key_ops"}
                or key["kty"] != "EC"
                or key["crv"] != "P-256"
                or key.get("alg", "ES256") != "ES256"
                or key.get("use", "sig") != "sig"
                or key.get("key_ops", ["verify"]) != ["verify"]
                or not isinstance(key["kid"], str)
                or not 1 <= len(key["kid"]) <= 128
                or key["kid"] in keys
            ):
                raise ValueError("invalid_jwks")
            x, y = decode64(key["x"]), decode64(key["y"])
            if len(x) != 32 or len(y) != 32:
                raise ValueError("invalid_jwks")
            keys[key["kid"]] = ec.EllipticCurvePublicNumbers(
                int.from_bytes(x, "big"), int.from_bytes(y, "big"), ec.SECP256R1()
            ).public_key()
        if not 0 <= self.clock() - observed_at <= 300:
            raise ValueError("stale_jwks")
        self.keys, self.refreshed = keys, observed_at

    def verify(
        self, token, *, organization_id, organization_name, group_id, group_name, machine_id
    ):
        try:
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.asymmetric import ec, utils

            now = self.clock()
            if self.refreshed is None or not 0 <= now - self.refreshed <= 3600:
                raise ValueError("stale_jwks")
            if not isinstance(token, str) or not 1 <= len(token) <= 1000:
                raise ValueError("invalid_token")
            header_part, claims_part, signature_part = token.split(".")
            header = strict_json(decode64(header_part))
            if (
                type(header) is not dict
                or set(header) - {"alg", "kid", "typ"}
                or header.get("alg") != "ES256"
                or header.get("typ", "JWT") != "JWT"
                or header.get("kid") not in self.keys
            ):
                raise ValueError("invalid_header")
            signature = decode64(signature_part)
            if len(signature) != 64:
                raise ValueError("invalid_signature")
            self.keys[header["kid"]].verify(
                utils.encode_dss_signature(
                    int.from_bytes(signature[:32], "big"), int.from_bytes(signature[32:], "big")
                ),
                (header_part + "." + claims_part).encode(),
                ec.ECDSA(hashes.SHA256()),
            )
            claims = strict_json(decode64(claims_part))
            if type(claims) is not dict:
                raise ValueError("invalid_claims")
            if any(type(claims.get(k)) is not int for k in ("iat", "nbf", "exp")):
                raise ValueError("invalid_time")
            if not (
                claims["iat"] <= claims["nbf"] <= now < claims["exp"]
                and 0 < claims["exp"] - claims["iat"] <= 300
                and now - claims["iat"] <= 300
            ):
                raise ValueError("invalid_time")
            expected = {
                "iss": ISSUER,
                "salad_organization_name": organization_name,
                "salad_workload_name": group_name,
            }
            if any(claims.get(k) != v for k, v in expected.items()):
                raise ValueError("invalid_scope")
            if (
                normalized_uuid(claims.get("aud")) != normalized_uuid(organization_id)
                or normalized_uuid(claims.get("salad_organization_id"))
                != normalized_uuid(organization_id)
                or normalized_uuid(claims.get("sub")) != normalized_uuid(machine_id)
                or normalized_uuid(claims.get("salad_machine_id")) != normalized_uuid(machine_id)
                or normalized_uuid(claims.get("salad_workload_id")) != normalized_uuid(group_id)
            ):
                raise ValueError("invalid_scope")
            if "salad_container_group_id" in claims and normalized_uuid(
                claims["salad_container_group_id"]
            ) != normalized_uuid(group_id):
                raise ValueError("invalid_scope")
            return True
        except Exception:
            raise ValueError("workload_identity_rejected") from None
