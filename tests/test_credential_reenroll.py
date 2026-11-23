"""Owner credential re-enrollment: fakes only, no network, synthetic secrets never surface."""

import contextlib
import io
import json
import os
import sys
import time
from types import SimpleNamespace

import pytest

from tessera_control import cli
from tessera_control import credential_reenroll as cr
from tessera_control.salad_secret_files import PrivateFileSecretResolver
from tessera_control.salad_secrets import SecretBuffer

REF = "credential-ref:salad-test"
OLD, NEW = b"synthetic-old-marker-0001", b"synthetic-new-marker-0002"


class Stdin:
    def __init__(self, data: bytes, tty: bool = False):
        self.buffer = io.BytesIO(data)
        self._tty = tty

    def isatty(self):
        return self._tty


@pytest.fixture
def store(tmp_path):
    path = tmp_path.resolve() / "credentials"
    path.mkdir(mode=0o700)
    receipts = tmp_path.resolve() / "receipts"
    receipts.mkdir()
    return path, receipts


def enroll_expired(path, version=1):
    past = time.time() - 7200
    resolver = PrivateFileSecretResolver(path, now=lambda: past)
    resolver.enroll(REF, version, SecretBuffer(bytearray(OLD)), expires_at=past + 3600)
    resolver.close()


def snapshot(path):
    return {n: (path / n).read_bytes() for n in sorted(os.listdir(path)) if n != ".lock"}


class FakeRead:
    def __init__(self, fail=None):
        self.fail, self.calls = fail, []

    def __call__(self, organization, secret):
        self.calls.append(organization)
        if self.fail:
            raise self.fail


class Rejected(Exception):
    reason = "credential_rejected"  # the provider refused the key (401/403)


class Outage(Exception):
    reason = "provider_unavailable"  # no usable answer: not a verdict on the key


def argv(path, receipts, *extra, organization=True):
    return [
        "credentials",
        "reenroll",
        "salad",
        "--credential-directory",
        str(path),
        "--reference",
        REF,
        *(["--organization", "acme-org"] if organization else []),
        *(["--receipt-directory", str(receipts)] if receipts else []),
        *extra,
    ]


def run(monkeypatch, capsys, args, data=NEW, tty=False, read=None):
    read = read or FakeRead()
    monkeypatch.setattr(sys, "stdin", Stdin(data, tty))
    monkeypatch.setattr("tessera_control.salad_http.check_management_credential", read)
    code = cli.main(args)
    out = capsys.readouterr()
    return code, out.out, out.err, read


def no_secret(*texts):
    for text in texts:
        for marker in (OLD, NEW):
            assert marker.decode() not in text


def test_happy_path_expired_to_next_version(store, monkeypatch, capsys):
    path, receipts = store
    enroll_expired(path)
    code, out, err, read = run(
        monkeypatch, capsys, argv(path, receipts, "--expires-in-hours", "72")
    )
    assert code == 0 and read.calls == ["acme-org"]
    (receipt_file,) = list(receipts.iterdir())
    assert oct(receipt_file.stat().st_mode & 0o777) == "0o600"
    text = receipt_file.read_text()
    receipt = json.loads(text)
    assert receipt["schema"] == cr.RECEIPT_SCHEMA and receipt["provider"] == "salad"
    assert (receipt["previous_version"], receipt["version"]) == (1, 2)
    assert receipt["validation"] == "ok" and receipt["reference"] == REF
    assert (receipt["health_before"], receipt["health_after"]) == ("expired", "ok")
    assert receipt["expires_at"] > time.time() and receipt["at"]
    assert "CLEARED" in err and "NOT CLEARED" not in err
    resolver = PrivateFileSecretResolver(path)
    assert resolver.metadata(REF).version == 2
    with resolver.resolve(REF, 2) as stored:
        assert bytes(stored._buffer) == NEW
    resolver.close()
    no_secret(out, err, text)


def test_receipt_to_stdout_without_directory(store, monkeypatch, capsys):
    path, _ = store
    enroll_expired(path)
    code, out, err, _ = run(monkeypatch, capsys, argv(path, None))
    assert code == 0 and json.loads(out)["version"] == 2
    no_secret(out, err)


def test_validation_rejection_stores_nothing(store, monkeypatch, capsys):
    path, receipts = store
    enroll_expired(path)
    before = snapshot(path)
    code, out, err, _ = run(monkeypatch, capsys, argv(path, receipts), read=FakeRead(Rejected()))
    assert code == 2 and "validation_rejected" in err
    assert snapshot(path) == before and not list(receipts.iterdir())
    no_secret(out, err)


@pytest.mark.parametrize(
    ("data", "tty", "category"),
    [
        (NEW, True, "stdin_is_a_terminal"),
        (b"a" * 4097, False, "bad_secret_shape"),
        (NEW + b"\nextra-line\n", False, "bad_secret_shape"),
        (b"", False, "bad_secret_shape"),
    ],
)
def test_bad_stdin_refused(store, monkeypatch, capsys, data, tty, category):
    path, receipts = store
    enroll_expired(path)
    before = snapshot(path)
    code, out, err, read = run(monkeypatch, capsys, argv(path, receipts), data, tty)
    assert code == 2 and category in err and not read.calls
    assert snapshot(path) == before
    no_secret(out, err)


def test_missing_and_revoked_refused_before_reading_stdin(store, monkeypatch, capsys):
    path, receipts = store
    stdin = Stdin(NEW)
    monkeypatch.setattr(sys, "stdin", stdin)
    assert cli.main(argv(path, receipts)) == 2
    assert "reference_not_found" in capsys.readouterr().err
    enroll_expired(path)
    resolver = PrivateFileSecretResolver(path)
    resolver.revoke(REF)
    resolver.close()
    assert cli.main(argv(path, receipts)) == 2
    assert "reference_revoked" in capsys.readouterr().err
    assert stdin.buffer.tell() == 0  # the secret was never read


def test_explicit_version_must_exceed_previous(store, monkeypatch, capsys):
    path, receipts = store
    enroll_expired(path, version=3)
    before = snapshot(path)
    code, _, err, _ = run(monkeypatch, capsys, argv(path, receipts, "--version", "3"))
    assert code == 2 and "version_not_greater" in err and snapshot(path) == before
    code, _, _, _ = run(monkeypatch, capsys, argv(path, receipts, "--version", "7"))
    assert code == 0
    assert json.loads(next(receipts.iterdir()).read_text())["version"] == 7


def test_version_mismatch_reported_with_next_step(store, monkeypatch, capsys, tmp_path):
    path, receipts = store
    enroll_expired(path)
    profile = SimpleNamespace(
        credential_reference=REF, credential_version=5, salad_organization="acme-org"
    )
    monkeypatch.setattr(
        "tessera_control.salad_campaign_profiles.load_profiles",
        lambda _path: SimpleNamespace(profiles=[profile]),
    )
    args = argv(path, receipts, "--profiles-file", str(tmp_path / "p.json"), organization=False)
    code, out, err, read = run(monkeypatch, capsys, args)
    assert code == 0 and read.calls == ["acme-org"]
    assert "NOT CLEARED (version_mismatch" in err
    assert "credential_version" in err and "tessera providers update" in err
    receipt = json.loads(next(receipts.iterdir()).read_text())
    assert (receipt["health_before"], receipt["health_after"]) == ("expired", "version_mismatch")
    no_secret(out, err)


def test_parser_has_no_secret_bearing_option():
    top = cli.parser()
    credentials = top._subparsers._group_actions[0].choices["credentials"]
    reenroll = credentials._subparsers._group_actions[0].choices["reenroll"]
    options = {s for a in reenroll._actions for s in a.option_strings}
    assert options == {
        "-h",
        "--help",
        "--credential-directory",
        "--reference",
        "--version",
        "--expires-in-hours",
        "--receipt-directory",
        "--profiles-file",
        "--organization",
    }
    assert {a.dest for a in reenroll._actions if not a.option_strings} == {"provider"}


def test_unknown_argument_never_echoed(store, monkeypatch, capsys):
    path, receipts = store
    args = [*argv(path, receipts), "--secret", "leaky-value-9"]
    code, out, err, _ = run(monkeypatch, capsys, args)
    assert code == 2 and "leaky-value-9" not in out + err


def test_secret_not_in_exception_or_repr(store):
    path, _ = store
    enroll_expired(path)
    ctx = cr.ReenrollContext(str(path), organization="acme-org", transport=FakeRead(Rejected()))
    with pytest.raises(cr.ReenrollRefused) as caught:
        cr.reenroll("salad", ctx, REF, stdin=Stdin(NEW))
    assert NEW.decode() not in str(caught.value) + repr(caught.value)
    assert caught.value.category == "validation_rejected"


def test_registry_accepts_second_provider_with_one_registration(tmp_path, monkeypatch):
    state = {"version": 1, "expires_at": time.time() - 5, "revoked": False}
    stored = []

    def put(secret, ref, version, expires_at):
        state.update(version=version, expires_at=expires_at)
        stored.append(version)

    adapter = cr.ReenrollAdapter("fake", lambda ref: dict(state), put, lambda s, i: None)
    monkeypatch.setitem(cr._ADAPTERS, "fake", lambda ctx: adapter)
    assert "fake" in cr.provider_ids()
    result = cr.reenroll(
        "fake",
        cr.ReenrollContext(str(tmp_path)),
        "ref:x",
        stdin=Stdin(NEW),
        receipt_directory=str(tmp_path),
        expires_in_hours=72,
    )
    assert stored == [2] and result["finding"] == "cleared"
    assert result["receipt"]["health_before"] == "expired"
    with pytest.raises(cr.ReenrollRefused):
        cr.reenroll("nope", cr.ReenrollContext(str(tmp_path)), "r", stdin=Stdin(NEW))


def test_the_default_expiry_clears_the_finding(store, monkeypatch, capsys):
    path, receipts = store
    enroll_expired(path)
    code, _, err, _ = run(monkeypatch, capsys, argv(path, receipts))
    assert code == 0 and "CLEARED (state ok)" in err and "NOT CLEARED" not in err


def test_a_short_expiry_reports_warning_not_cleared(store, monkeypatch, capsys):
    path, receipts = store
    enroll_expired(path)
    code, _, err, _ = run(monkeypatch, capsys, argv(path, receipts, "--expires-in-hours", "24"))
    assert code == 0 and "NOT CLEARED (warning" in err and "--expires-in-hours" in err


def test_an_outage_is_not_reported_as_a_rejected_key(store, monkeypatch, capsys):
    path, receipts = store
    enroll_expired(path)
    before = snapshot(path)
    code, out, err, _ = run(monkeypatch, capsys, argv(path, receipts), read=FakeRead(Outage()))
    assert code == 2 and "validation_provider_unavailable" in err and "rejected" not in err
    assert snapshot(path) == before
    no_secret(out, err)


def test_a_success_tells_the_owner_to_delete_the_old_key_at_the_provider(
    store, monkeypatch, capsys
):
    path, receipts = store
    enroll_expired(path)
    code, _, err, _ = run(monkeypatch, capsys, argv(path, receipts))
    assert code == 0 and "delete the old key in the Salad console" in err


def test_a_failed_receipt_write_after_the_store_is_its_own_exit_code(store, monkeypatch, capsys):
    path, receipts = store
    enroll_expired(path)
    monkeypatch.setattr(
        cr,
        "_write_receipt",
        lambda *a, **k: (_ for _ in ()).throw(cr.ReenrollRefused("receipt_unwritable")),
    )
    code, out, err, _ = run(monkeypatch, capsys, argv(path, receipts))
    assert code == 3  # not 2: the version was stored
    assert "stored_receipt_unwritable" in err and "IS stored" in err
    assert json.loads(out)["version"] == 2  # the record is printed so it is not lost
    resolver = PrivateFileSecretResolver(path)
    assert resolver.read_metadata(REF)["version"] == 2
    resolver.close()
    no_secret(out, err)


def test_a_revocation_during_validation_is_not_undone_by_the_enrollment(store, monkeypatch, capsys):
    path, receipts = store
    enroll_expired(path)

    class RevokesWhileChecking(FakeRead):
        def __call__(self, organization, secret):
            resolver = PrivateFileSecretResolver(path)
            resolver.revoke(REF)  # lands inside the validation window
            resolver.close()

    code, out, err, _ = run(monkeypatch, capsys, argv(path, receipts), read=RevokesWhileChecking())
    assert code == 2 and "enrollment_rejected" in err
    resolver = PrivateFileSecretResolver(path)
    assert resolver.read_metadata(REF)["revoked"] is True  # still revoked, version unchanged
    assert resolver.read_metadata(REF)["version"] == 1
    resolver.close()
    no_secret(out, err)


@pytest.mark.parametrize(
    "words",
    [
        ["credentials", "reenroll", "salad", "--reference", REF, "sk-LEAKED-SECRET"],
        ["credentials", "reenroll", "salad", "--secret=sk-LEAKED2"],
        ["credentials", "sk-LEAKED3"],
        ["credentials", "--token", "sk-LEAKED4"],
    ],
)
def test_the_installed_entry_point_never_echoes_an_unknown_argument(monkeypatch, capsys, words):
    # The installed `tessera` calls main() with no argument and reads sys.argv itself.
    monkeypatch.setattr(sys, "argv", ["tessera", *words])
    with contextlib.suppress(SystemExit):
        code = cli.main()
        assert code == 2
    seen = capsys.readouterr()
    assert "LEAKED" not in seen.out + seen.err
