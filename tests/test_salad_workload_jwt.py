import base64
import json

import pytest

pytest.importorskip("cryptography")
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils

from tessera_control.salad_workload_jwt import ISSUER, SaladWorkloadJWT

ORG = "a" * 32
GROUP = "b" * 32
MACHINE = "c" * 32


def b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


class IsolatedToken(str):
    def __repr__(self):
        return "<isolated-workload-token redacted>"


def key_document(key):
    p = key.public_key().public_numbers()
    return {
        "keys": [
            {
                "kid": "test",
                "kty": "EC",
                "crv": "P-256",
                "alg": "ES256",
                "x": b64(p.x.to_bytes(32, "big")),
                "y": b64(p.y.to_bytes(32, "big")),
            }
        ]
    }


def claims(now=100):
    return dict(
        iss=ISSUER,
        aud=ORG,
        sub=MACHINE,
        salad_organization_id=ORG,
        salad_organization_name="acme",
        salad_machine_id=MACHINE,
        salad_workload_id=GROUP,
        salad_workload_name="attempt-operation",
        iat=int(now),
        nbf=int(now),
        exp=int(now) + 300,
    )


def signed(key, payload, header=None):
    header = header or {"alg": "ES256", "kid": "test", "typ": "JWT"}
    first = b64(json.dumps(header, separators=(",", ":")).encode())
    second = b64(json.dumps(payload, separators=(",", ":")).encode())
    message = (first + "." + second).encode()
    r, s = utils.decode_dss_signature(key.sign(message, ec.ECDSA(hashes.SHA256())))
    return IsolatedToken(
        message.decode() + "." + b64(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    )


def verify(verifier, token):
    return verifier.verify(
        token,
        organization_id=ORG,
        organization_name="acme",
        group_id=GROUP,
        group_name="attempt-operation",
        machine_id=MACHINE,
    )


@pytest.fixture
def jwt_case():
    key = ec.generate_private_key(ec.SECP256R1())
    clock = [100.0]
    v = SaladWorkloadJWT(clock=lambda: clock[0])
    v.load(key_document(key), observed_at=100)
    return key, v, clock


def test_real_es256_valid_and_bad_signature(jwt_case):
    key, v, _ = jwt_case
    assert verify(v, signed(key, claims()))
    other = ec.generate_private_key(ec.SECP256R1())
    with pytest.raises(ValueError, match="identity_rejected"):
        verify(v, signed(other, claims()))


@pytest.mark.parametrize(
    "field,value",
    [
        ("iss", "https://attacker.invalid"),
        ("aud", "d" * 32),
        ("sub", "d" * 32),
        ("salad_machine_id", "d" * 32),
        ("salad_organization_id", "d" * 32),
        ("salad_workload_id", "d" * 32),
        ("salad_workload_name", "foreign"),
        ("salad_organization_name", "foreign"),
        ("iat", True),
        ("nbf", 101),
        ("exp", 100),
        ("exp", 401),
        ("salad_container_group_id", "d" * 32),
    ],
)
def test_exact_claims_and_five_minute_scope(jwt_case, field, value):
    key, v, _ = jwt_case
    payload = claims()
    payload[field] = value
    with pytest.raises(ValueError, match="identity_rejected"):
        verify(v, signed(key, payload))


@pytest.mark.parametrize(
    "header",
    [
        {"alg": "none", "kid": "test"},
        {"alg": "HS256", "kid": "test"},
        {"alg": "ES256", "kid": "unknown"},
        {"alg": "ES256", "kid": "test", "jku": "https://attacker.invalid"},
        {"alg": "ES256", "kid": "test", "x5u": "https://attacker.invalid"},
    ],
)
def test_header_never_selects_fetch(jwt_case, header):
    key, v, _ = jwt_case
    v.factory = lambda *a, **kw: pytest.fail("header-driven fetch")
    with pytest.raises(ValueError, match="identity_rejected"):
        verify(v, signed(key, claims(), header))


def test_stale_cache_and_rotation(jwt_case):
    key, v, clock = jwt_case
    token = signed(key, claims())
    clock[0] = 4000
    with pytest.raises(ValueError):
        verify(v, token)
    rotated = ec.generate_private_key(ec.SECP256R1())
    v.load(key_document(rotated), observed_at=clock[0])
    assert verify(v, signed(rotated, claims(clock[0])))


def test_jwks_rejects_private_and_wrong_curve(jwt_case):
    key, v, _ = jwt_case
    doc = key_document(key)
    doc["keys"][0]["d"] = "do-not-accept-private-key"
    with pytest.raises(ValueError):
        v.load(doc, observed_at=100)
    doc = key_document(key)
    doc["keys"][0]["crv"] = "P-384"
    with pytest.raises(ValueError):
        v.load(doc, observed_at=100)
