"""Owner-run helpers carry the automation identity and the delegation reference by themselves."""

from __future__ import annotations

import json

import pytest

from tessera_control import agent_research_review, cli
from tessera_control.approval_trust import AUTOMATION_HEADER, DELEGATION_HEADER
from tessera_control.owner_identity import (
    DELEGATION_ENV,
    LABEL_ENV,
    IdentityError,
    OwnerIdentity,
    owner_request_headers,
    resolve_identity,
)
from tessera_control.provider_trust_cli import run_provider_trust

DIGEST = "a" * 64


class _Response:
    status = 200

    def __init__(self, body):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, _limit=-1):
        return json.dumps(self._body).encode()


class _Opener:
    requests: list = []

    def open(self, request, timeout=None):
        _Opener.requests.append(request)
        return _Response({"ok": True})


@pytest.fixture(autouse=True)
def _wire(monkeypatch):
    _Opener.requests = []
    monkeypatch.setattr(
        agent_research_review.urllib.request, "build_opener", lambda *a, **k: _Opener()
    )
    monkeypatch.delenv(LABEL_ENV, raising=False)
    monkeypatch.delenv(DELEGATION_ENV, raising=False)


@pytest.fixture
def owner_header(tmp_path):
    path = tmp_path / "owner.header"
    path.write_text("Authorization: Bearer synthetic-owner\n")
    path.chmod(0o600)
    return path


def _headers():
    assert _Opener.requests, "no request was made"
    return {k.lower(): v for k, v in _Opener.requests[-1].header_items()}


def _campaign(argv, owner_header, capsys):
    arguments = cli.parser().parse_args(["campaign", *argv, "--owner-header", str(owner_header)])
    code = cli.run_campaign(arguments)
    return code, capsys.readouterr()


APPROVE = [
    "approve", "--campaign-id", "c1", "--expected-revision", "1",
    "--expected-spec-digest", DIGEST, "--idempotency-key", "k",
]  # fmt: skip


CANCEL = [
    "cancel", "--campaign-id", "c1", "--expected-revision", "1", "--reason", "r",
    "--idempotency-key", "k",
]  # fmt: skip

READ_ONLY = [
    ["get", "--campaign-id", "c1"],
    ["list", "--project-id", "p"],
    ["receipt", "--campaign-id", "c1"],
    ["report", "--campaign-id", "c1", "--format", "json"],
    ["standing", "list", "--project-id", "p"],
    ["standing", "show", "--project-id", "p", "--id", "s1"],
]


def test_resolution_precedence_and_defaults():
    ns = type("A", (), {"automation_label": None, "delegation_ref": None})()
    assert resolve_identity(ns, "campaign-submit", {}) == OwnerIdentity(
        "tessera-cli:campaign-submit"
    )
    env = {LABEL_ENV: "from-env", DELEGATION_ENV: "deleg-env"}
    assert resolve_identity(ns, "x", env) == OwnerIdentity("from-env", "deleg-env")
    flagged = type("A", (), {"automation_label": "from-flag", "delegation_ref": "deleg-flag"})()
    assert resolve_identity(flagged, "x", env) == OwnerIdentity("from-flag", "deleg-flag")


@pytest.mark.parametrize("bad", [" padded", "x" * 129, "tab\tinside", "trail "])
def test_invalid_label_or_reference_is_refused_without_echo(bad):
    ns = type("A", (), {"automation_label": bad, "delegation_ref": None})()
    with pytest.raises(IdentityError) as error:
        resolve_identity(ns, "x", {})
    assert repr(bad) not in str(error.value) and "x" * 129 not in str(error.value)
    ns = type("A", (), {"automation_label": None, "delegation_ref": bad})()
    with pytest.raises(IdentityError):
        resolve_identity(ns, "x", {})


def test_external_helper_headers():
    identity = OwnerIdentity("snapshot-upload", "deleg-1")
    headers = owner_request_headers("Bearer t", identity, "key-1")
    assert headers[AUTOMATION_HEADER] == "snapshot-upload"
    assert headers[DELEGATION_HEADER] == "deleg-1"
    assert headers["Idempotency-Key"] == "key-1" and headers["Authorization"] == "Bearer t"
    assert DELEGATION_HEADER not in owner_request_headers("Bearer t", OwnerIdentity("l"))


def test_approve_sends_the_default_label_and_no_invented_delegation(owner_header, capsys):
    code, out = _campaign(APPROVE, owner_header, capsys)
    headers = _headers()
    assert code == 0
    assert headers[AUTOMATION_HEADER.lower()] == "tessera-cli:campaign-approve"
    assert DELEGATION_HEADER.lower() not in headers
    assert "synthetic-owner" not in out.out + out.err


def test_flag_and_delegation_ref_reach_the_request(owner_header, capsys):
    code, _ = _campaign(
        [*APPROVE, "--automation-label", "launch-helper", "--delegation-ref", "deleg-7"],
        owner_header,
        capsys,
    )
    headers = _headers()
    assert code == 0
    assert headers[AUTOMATION_HEADER.lower()] == "launch-helper"
    assert headers[DELEGATION_HEADER.lower()] == "deleg-7"


def test_environment_supplies_both(owner_header, capsys, monkeypatch):
    monkeypatch.setenv(LABEL_ENV, "env-helper")
    monkeypatch.setenv(DELEGATION_ENV, "deleg-env")
    code, _ = _campaign(CANCEL, owner_header, capsys)
    headers = _headers()
    assert code == 0
    assert headers[AUTOMATION_HEADER.lower()] == "env-helper"
    assert headers[DELEGATION_HEADER.lower()] == "deleg-env"


def test_submit_carries_the_identity(tmp_path, owner_header, capsys):
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"name": "n"}))
    _campaign(
        ["submit", "--project-id", "p", "--spec", str(spec), "--idempotency-key", "k"],
        owner_header,
        capsys,
    )
    # an invalid spec stops before any request; a valid shape reaches the transport
    if _Opener.requests:
        assert _headers()[AUTOMATION_HEADER.lower()] == "tessera-cli:campaign-submit"


def test_a_bad_label_stops_before_any_request(owner_header, capsys):
    code, out = _campaign([*APPROVE, "--automation-label", " bad"], owner_header, capsys)
    assert code == 2 and not _Opener.requests
    assert "synthetic-owner" not in out.out + out.err


def test_provider_trust_reload_carries_the_identity(owner_header, capsys):
    arguments = cli.parser().parse_args(
        [
            "admin", "provider-trust", "reload", "--idempotency-key", "k1",
            "--owner-header", str(owner_header), "--delegation-ref", "deleg-pt",
        ]
    )  # fmt: skip
    assert run_provider_trust(arguments) == 0
    headers = _headers()
    assert headers[AUTOMATION_HEADER.lower()] == "tessera-cli:admin-provider-trust-reload"
    assert headers[DELEGATION_HEADER.lower()] == "deleg-pt"
    capsys.readouterr()


def test_provider_trust_bad_label_exits_2(owner_header, capsys):
    arguments = cli.parser().parse_args(
        [
            "admin", "provider-trust", "reload", "--idempotency-key", "k1",
            "--owner-header", str(owner_header), "--automation-label", "x" * 200,
        ]
    )  # fmt: skip
    assert run_provider_trust(arguments) == 2 and not _Opener.requests
    capsys.readouterr()


def test_standing_revoke_carries_the_identity(owner_header, capsys):
    code, _ = _campaign(
        ["standing", "revoke", "--project-id", "p", "--id", "s1", "--expected-revision", "1",
         "--reason", "done", "--idempotency-key", "k"],
        owner_header,
        capsys,
    )  # fmt: skip
    assert code == 0
    assert _headers()[AUTOMATION_HEADER.lower()] == "tessera-cli:campaign-standing"


@pytest.mark.parametrize("argv", READ_ONLY)
def test_reads_ignore_an_invalid_environment_label(argv, owner_header, capsys, monkeypatch):
    monkeypatch.setenv(LABEL_ENV, "bad label \x07 secret-looking-value ")
    monkeypatch.setenv(DELEGATION_ENV, "x" * 200)
    code, out = _campaign(argv, owner_header, capsys)
    assert code == 0 and _Opener.requests
    assert "secret-looking-value" not in out.out + out.err


@pytest.mark.parametrize(
    "argv",
    [
        CANCEL,
        APPROVE,
        [
            "standing",
            "revoke",
            "--project-id",
            "p",
            "--id",
            "s1",
            "--expected-revision",
            "1",
            "--reason",
            "done",
            "--idempotency-key",
            "k",
        ],
    ],  # fmt: skip
)
def test_mutations_still_refuse_an_invalid_environment_label(
    argv, owner_header, capsys, monkeypatch
):
    monkeypatch.setenv(LABEL_ENV, " secret-looking-value")
    code, out = _campaign(argv, owner_header, capsys)
    assert code == 2 and not _Opener.requests
    assert "secret-looking-value" not in out.out + out.err


def test_delegation_without_label_is_refused_only_for_mutations(owner_header, capsys):
    # The default label stands in, so a delegation alone is still a valid mutation identity.
    code, _ = _campaign([*CANCEL, "--delegation-ref", "d1"], owner_header, capsys)
    assert code == 0
    assert _headers()[AUTOMATION_HEADER.lower()] == "tessera-cli:campaign-cancel"
    _Opener.requests.clear()
    code, _ = _campaign(
        ["get", "--campaign-id", "c1", "--delegation-ref", "d1"], owner_header, capsys
    )
    assert code == 0
    assert AUTOMATION_HEADER.lower() not in _headers()


def test_provider_trust_show_ignores_a_bad_label_but_reload_does_not(
    owner_header, capsys, monkeypatch
):
    monkeypatch.setenv(LABEL_ENV, " secret-looking-value")
    show = cli.parser().parse_args(
        ["admin", "provider-trust", "show", "--owner-header", str(owner_header)]
    )
    assert run_provider_trust(show) == 0 and _Opener.requests
    _Opener.requests.clear()
    reload = cli.parser().parse_args(
        ["admin", "provider-trust", "reload", "--idempotency-key", "k1",
         "--owner-header", str(owner_header)]
    )  # fmt: skip
    assert run_provider_trust(reload) == 2 and not _Opener.requests
    assert "secret-looking-value" not in "".join(capsys.readouterr())
