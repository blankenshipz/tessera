"""Rotation and renewal of an enrolled Salad management credential (ADR 0103).

Synthetic markers only. The secret never reaches an output, an exception text or argv; the old
version stops resolving and the new one resolves.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import time

import pytest

from tessera_control.salad_secret_files import PrivateFileSecretResolver
from tessera_control.salad_secrets import SecretBuffer
from tessera_operator import salad_enroll

REF = "credential-ref:salad-test"
OLD, NEW = b"synthetic-old-marker-0001", b"synthetic-new-marker-0002"


class Stdin:
    def __init__(self, data: bytes, tty: bool = False):
        self.buffer = io.BytesIO(data)
        self._tty = tty

    def isatty(self):
        return self._tty


@pytest.fixture
def credentials(tmp_path):
    path = tmp_path.resolve() / "credentials"
    path.mkdir(mode=0o700)
    return path


def enroll(credentials, secret=OLD, *, expires_in=3600.0, now=None, version=1):
    resolver = PrivateFileSecretResolver(credentials, **({"now": now} if now else {}))
    resolver.enroll(
        REF, version, SecretBuffer(bytearray(secret)), expires_at=(now or time.time)() + expires_in
    )
    resolver.close()


def rotate(credentials, stdin, capsys, monkeypatch, *extra):
    monkeypatch.setattr(sys, "stdin", stdin)
    code = salad_enroll.main(
        ["rotate", "--credential-directory", str(credentials), "--reference", REF, *extra]
    )
    out = capsys.readouterr()
    return code, out.out, out.err


def resolve(credentials, version):
    resolver = PrivateFileSecretResolver(credentials)
    try:
        return resolver.resolve(REF, version)
    finally:
        resolver.close()


def test_rotate_from_stdin_bumps_version_and_expiry_and_prints_no_secret(
    credentials, capsys, monkeypatch
):
    enroll(credentials, expires_in=60)
    before = PrivateFileSecretResolver(credentials).metadata(REF)
    code, out, err = rotate(
        credentials,
        Stdin(NEW + b"\n"),
        capsys,
        monkeypatch,
        "--secret-stdin",
        "--expires-in-hours",
        "48",
    )
    assert code == 0
    printed = json.loads(out)
    assert set(printed) == {"reference", "previous_version", "version", "expires_at"}
    assert printed["previous_version"] == 1 and printed["version"] == 2
    assert printed["expires_at"] > before.expires_at + 40 * 3600
    assert "credential_version" in err and "2" in err
    for text in (out, err):
        assert NEW.decode() not in text and OLD.decode() not in text
    with pytest.raises(ValueError):
        resolve(credentials, 1)  # the old version no longer resolves
    secret = resolve(credentials, 2)
    assert secret.contains_in(NEW) and not secret.contains_in(OLD)
    secret.zeroize()


def test_an_expired_reference_can_be_rotated(credentials, capsys, monkeypatch):
    past = lambda: time.time() - 7200  # noqa: E731
    enroll(credentials, now=past, expires_in=3600)  # expired an hour ago
    assert PrivateFileSecretResolver(credentials).metadata(REF).expires_at < time.time()
    code, out, _ = rotate(credentials, Stdin(NEW), capsys, monkeypatch, "--secret-stdin")
    assert code == 0 and json.loads(out)["version"] == 2
    resolve(credentials, 2).zeroize()


@pytest.mark.parametrize(
    "data",
    [b"", b"\n", b"has space", "café-marker".encode(), b"x" * 4097, b"bad\x00byte"],
)
def test_a_malformed_secret_is_refused_without_echo(credentials, capsys, monkeypatch, data):
    enroll(credentials)
    code, out, err = rotate(credentials, Stdin(data), capsys, monkeypatch, "--secret-stdin")
    assert code == 2 and out == ""
    assert "bad_secret_shape" in err
    printable = data.decode("utf-8", "ignore").strip()
    assert not printable or printable not in err  # the refused input is never echoed
    assert resolve(credentials, 1).contains_in(OLD)  # untouched


def test_a_terminal_on_stdin_is_refused(credentials, capsys, monkeypatch):
    enroll(credentials)
    code, _, err = rotate(credentials, Stdin(NEW, tty=True), capsys, monkeypatch, "--secret-stdin")
    assert code == 2 and "stdin_is_a_terminal" in err
    resolve(credentials, 1).zeroize()


def test_a_missing_or_revoked_reference_is_refused_before_stdin_is_read(
    credentials, capsys, monkeypatch
):
    stdin = Stdin(NEW)
    code, _, err = rotate(credentials, stdin, capsys, monkeypatch, "--secret-stdin")
    assert code == 2 and "reference_not_found" in err
    assert stdin.buffer.tell() == 0  # nothing consumed
    enroll(credentials)
    resolver = PrivateFileSecretResolver(credentials)
    resolver.revoke(REF)
    resolver.close()
    code, _, err = rotate(credentials, Stdin(NEW), capsys, monkeypatch, "--secret-stdin")
    assert code == 2 and "reference_revoked" in err


def test_without_stdin_flag_the_hidden_prompt_is_used(credentials, capsys, monkeypatch):
    enroll(credentials)
    monkeypatch.setattr(
        salad_enroll, "hidden_secret", lambda: SecretBuffer(bytearray(b"synthetic-prompted-3"))
    )
    code, out, _ = rotate(credentials, Stdin(b"ignored"), capsys, monkeypatch)
    assert code == 0 and json.loads(out)["version"] == 2


def test_a_secret_argument_is_rejected_without_echo(credentials):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tessera_operator.salad_enroll",
            "rotate",
            "--credential-directory",
            str(credentials),
            "--reference",
            REF,
            "--key",
            "synthetic-argv-marker",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "synthetic-argv-marker" not in result.stdout + result.stderr


def test_renew_extends_the_same_key_and_takes_no_secret(credentials, capsys, monkeypatch):
    enroll(credentials, expires_in=600)
    monkeypatch.setattr(sys, "stdin", Stdin(b"must not be read"))
    code = salad_enroll.main(
        ["renew", "--credential-directory", str(credentials), "--reference", REF,
         "--expires-in-hours", "12"]
    )  # fmt: skip
    out = capsys.readouterr()
    assert code == 0
    printed = json.loads(out.out)
    assert printed["previous_version"] == 1 and printed["version"] == 2
    assert printed["expires_at"] > time.time() + 11 * 3600
    assert resolve(credentials, 2).contains_in(OLD)  # same key bytes, next version
    for text in (out.out, out.err):
        assert OLD.decode() not in text


def test_renew_is_bounded_and_refuses_a_shorter_window(credentials, capsys, monkeypatch):
    enroll(credentials, expires_in=20 * 3600)
    for hours in ("0", "25"):
        code = salad_enroll.main(
            ["renew", "--credential-directory", str(credentials), "--reference", REF,
             "--expires-in-hours", hours]
        )  # fmt: skip
        assert code == 2
    code = salad_enroll.main(
        ["renew", "--credential-directory", str(credentials), "--reference", REF,
         "--expires-in-hours", "1"]
    )  # fmt: skip
    assert code == 2 and "renewal_rejected" in capsys.readouterr().err  # not an extension
    resolve(credentials, 1).zeroize()  # unchanged


def test_the_create_only_enrollment_invocation_is_unchanged(credentials, capsys):
    assert (
        salad_enroll.enroll_main(["--credential-directory", str(credentials), "--reference", REF])
        == 2
    )  # a non-terminal still refuses to enroll
    assert not (credentials / "salad-test.credential").exists()
