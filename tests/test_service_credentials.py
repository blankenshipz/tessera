"""Installing and admitting a service credential: the secret is never an argument, a variable, a
log line, an error message or a receipt field. Real files in a temporary directory; no root needed
(``require_root=False`` makes the current user the store's owner)."""

from __future__ import annotations

import io
import json
import os
import re
from pathlib import Path

import pytest

from tessera_agent import service_credentials as sc
from tessera_agent.model_destinations import FORBIDDEN_ENVIRONMENT
from tessera_agent.service_credentials import CredentialRefused

SERVICE, NAME = "tessera-reasoning", "anthropic-api-key"
SECRET = b"sk-ant-SYNTHETIC-0123456789abcdefghij"
SENTINEL = SECRET.decode()


@pytest.fixture
def store(tmp_path):
    directory = tmp_path / "credentials"
    directory.mkdir(mode=0o755)
    os.chmod(directory, 0o755)
    units = tmp_path / "units"
    units.mkdir()
    return directory, units


def run_install(store, secret=SECRET, **over):
    directory, units = store
    options = dict(
        credentials_dir=directory,
        unit_dirs=[units],
        environ={},
        owner_label="owner1",
        reference_label="ref1",
        require_root=False,
    )
    options.update(over)
    return sc.install(SERVICE, NAME, io.BytesIO(secret), **options)


def every_file_text(*roots: Path) -> str:
    out = []
    for root in roots:
        for path in root.rglob("*"):
            if path.is_file() and path.name != "reasoning-anthropic-api-key":
                out.append(path.read_text(errors="replace"))
    return "\n".join(out)


def declare(units, directory, extra="", reference=True):
    lines = f"[Service]\nLoadCredential={NAME}:{directory}/reasoning-anthropic-api-key\n"
    if reference:
        lines += f"LoadCredential=model-credential:{directory}/reasoning-model-credential\n"
    (units / "tessera-reasoning.service").write_text(lines + extra)


# -- install ---------------------------------------------------------------------------------


def test_install_writes_an_owner_only_file_and_a_receipt_without_the_secret(store, capsys):
    directory, units = store
    receipt = run_install(store)
    target = directory / "reasoning-anthropic-api-key"
    info = target.stat()
    assert info.st_mode & 0o777 == 0o600 and info.st_uid == os.getuid() and info.st_nlink == 1
    assert target.read_bytes() == SECRET + b"\n"
    assert receipt["reference"] == f"{NAME}://owner1/ref1"
    assert receipt["fingerprint"] == sc.fingerprint(SECRET) and receipt["size"] == len(SECRET)
    assert receipt["unit_declared"] is None and receipt["replaced"] is False
    text = every_file_text(directory, units) + json.dumps(receipt) + capsys.readouterr().out
    assert SENTINEL not in text
    assert not any(SENTINEL[i : i + 8] in text for i in range(len(SENTINEL) - 7))
    assert [p.name for p in directory.iterdir() if p.name.startswith(".")] == []


def test_a_forbidden_variable_in_the_installers_environment_is_refused(store):
    directory, _ = store
    for name in FORBIDDEN_ENVIRONMENT:
        with pytest.raises(CredentialRefused) as refused:
            run_install(store, environ={name: ""})
        assert refused.value.code == "forbidden_environment"
    assert list(directory.iterdir()) == []


def test_a_forbidden_variable_in_a_unit_dropin_or_environment_file_is_refused(store, tmp_path):
    directory, units = store
    dropin = units / "tessera-reasoning.service.d"
    dropin.mkdir()
    (dropin / "x.conf").write_text('[Service]\nEnvironment="ANTHROPIC_API_KEY=" "OTHER=1"\n')
    with pytest.raises(CredentialRefused) as refused:
        run_install(store)
    assert refused.value.code == "unit_environment_forbidden"
    (dropin / "x.conf").unlink()
    envfile = tmp_path / "service.env"
    envfile.write_text("# note\nexport CLAUDE_API_KEY=value-never-shown\n")
    (dropin / "y.conf").write_text(f"[Service]\nEnvironmentFile=-{envfile}\n")
    with pytest.raises(CredentialRefused) as refused:
        run_install(store)
    assert refused.value.code == "unit_environment_forbidden"
    assert "value-never-shown" not in str(refused.value)
    assert list(directory.iterdir()) == []


def test_there_is_no_way_to_pass_the_secret_as_an_argument():
    with pytest.raises(SystemExit):
        sc.main(
            [
                "install",
                "--service",
                SERVICE,
                "--name",
                NAME,
                "--owner",
                "o",
                "--reference",
                "r",
                "--value",
                SENTINEL,
            ]  # fmt: skip
        )
    with pytest.raises(SystemExit):
        sc._parser().parse_args(["install", "--service", SERVICE, "--name", NAME, SENTINEL])


def test_a_terminal_stream_reads_without_echo(store, monkeypatch):
    calls = []

    class Tty(io.BytesIO):
        def isatty(self):
            return True

    monkeypatch.setattr(sc.getpass, "getpass", lambda prompt="": calls.append(prompt) or SENTINEL)
    directory, units = store
    receipt = sc.install(
        SERVICE, NAME, Tty(b"ignored"), credentials_dir=directory, unit_dirs=[units], environ={},
        owner_label="o", reference_label="r", require_root=False,
    )  # fmt: skip
    assert calls == [""] and receipt["fingerprint"] == sc.fingerprint(SECRET)
    assert (directory / "reasoning-anthropic-api-key").read_bytes() == SECRET + b"\n"


@pytest.mark.parametrize(
    "secret",
    [
        b"",
        b"\n",
        b"x" * 513,
        b"x" * 4097,
        b"has space in it",
        "sk-ant-é-bad".encode(),
        b"tab\tinside",
    ],
)
def test_a_malformed_secret_is_refused_without_echoing_bytes(store, secret):
    with pytest.raises(CredentialRefused) as refused:
        run_install(store, secret=secret)
    assert refused.value.code in {"invalid_credential_shape", "secret_not_provided"}
    shown = str(refused.value)
    assert secret.decode(errors="ignore")[:6] not in shown or not secret
    assert list(store[0].iterdir()) == []


def test_an_existing_credential_needs_replace_and_the_receipts_chain(store):
    directory, _ = store
    first = run_install(store)
    with pytest.raises(CredentialRefused) as refused:
        run_install(store, secret=b"sk-ant-SECOND-0123456789abcdef")
    assert refused.value.code == "exists"
    assert (directory / "reasoning-anthropic-api-key").read_bytes() == SECRET + b"\n"
    second = run_install(store, secret=b"sk-ant-SECOND-0123456789abcdef", replace=True)
    assert second["replaced"] is True and second["previous_hash"] == first["hash"]
    rows = sc.read_receipts(directory / sc.RECEIPTS_NAME)
    assert [r["hash"] for r in rows] == [first["hash"], second["hash"]]
    assert all(sc._receipt_hash(r) == r["hash"] for r in rows)
    assert [p.name for p in directory.iterdir() if p.name.endswith(".prev")] == []


def test_an_unsafe_store_is_refused(store, tmp_path):
    directory, units = store
    os.chmod(directory, 0o775)  # group-writable
    with pytest.raises(CredentialRefused) as refused:
        run_install(store)
    assert refused.value.code == "unsafe_permissions"
    os.chmod(directory, 0o755)
    link = tmp_path / "link"
    link.symlink_to(directory)
    with pytest.raises(CredentialRefused) as refused:
        run_install(store, credentials_dir=link)
    assert refused.value.code == "unsafe_permissions"
    (directory / "reasoning-anthropic-api-key").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(CredentialRefused):
        run_install(store, replace=True)
    assert not (tmp_path / "elsewhere").exists()


def test_a_unit_without_the_credential_declaration_is_refused_before_writing(store):
    directory, units = store
    (units / "tessera-reasoning.service").write_text("[Service]\nUser=x\n")
    with pytest.raises(CredentialRefused) as refused:
        run_install(store)
    assert refused.value.code == "not_declared_in_unit"
    assert list(directory.iterdir()) == []
    declare(units, directory / "elsewhere")  # declared with other paths
    with pytest.raises(CredentialRefused):
        run_install(store)
    assert list(directory.iterdir()) == []
    declare(units, directory, reference=False)  # the reference file is not declared
    with pytest.raises(CredentialRefused) as refused:
        run_install(store)
    assert refused.value.code == "not_declared_in_unit"
    assert list(directory.iterdir()) == []


def test_a_unit_that_declares_the_credential_is_accepted(store):
    directory, units = store
    declare(units, directory)
    assert run_install(store)["unit_declared"] is True


def test_an_unknown_service_or_credential_or_label_is_refused(store):
    directory, units = store
    with pytest.raises(CredentialRefused) as refused:
        sc.install("nope", NAME, io.BytesIO(SECRET), owner_label="o", reference_label="r")
    assert refused.value.code == "unknown_service"
    with pytest.raises(CredentialRefused) as refused:
        sc.install(SERVICE, "nope", io.BytesIO(SECRET), owner_label="o", reference_label="r")
    assert refused.value.code == "unknown_credential"
    with pytest.raises(CredentialRefused) as refused:
        run_install(store, owner_label="bad label")
    assert refused.value.code == "invalid_label"


def test_a_failed_receipt_leaves_no_partial_install(store, monkeypatch):
    directory, _ = store
    monkeypatch.setattr(sc, "_append_receipt", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    with pytest.raises(CredentialRefused):
        run_install(store)
    assert list(directory.iterdir()) == []
    monkeypatch.undo()
    run_install(store)
    monkeypatch.setattr(sc, "_append_receipt", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    with pytest.raises(CredentialRefused):
        run_install(store, secret=b"sk-ant-SECOND-0123456789abcdef", replace=True)
    assert (directory / "reasoning-anthropic-api-key").read_bytes() == SECRET + b"\n"
    assert (directory / "reasoning-model-credential").read_text() == f"{NAME}://owner1/ref1\n"


def test_the_command_line_installs_from_standard_input_and_prints_no_secret(
    store, monkeypatch, capsys
):
    directory, units = store

    class Stdin:
        buffer = io.BytesIO(SECRET + b"\n")

    monkeypatch.setattr(sc.sys, "stdin", Stdin)
    code = sc.main(
        [
            "install",
            "--service",
            SERVICE,
            "--name",
            NAME,
            "--owner",
            "o",
            "--reference",
            "r",
            "--credentials-dir",
            str(directory),
            "--user-store",
            "--unit-dir",
            str(units),
        ]  # fmt: skip
    )
    out = capsys.readouterr()
    assert code == 0 and SENTINEL not in out.out + out.err
    assert json.loads(out.out)["reference"] == f"{NAME}://o/r"
    where = ["--service", SERVICE, "--credentials-dir", str(directory), "--user-store"]
    assert sc.main(["check", *where, "--unit-dir", str(units)]) == 0
    checked = json.loads(capsys.readouterr().out)
    assert checked["ok"] and SENTINEL not in json.dumps(checked)
    assert sc.main(["list", *where]) == 0
    assert sc.main(["check", "--service", "nope"]) == sc.EXIT_REFUSED


# -- the flow and the runtime's key shape -----------------------------------------------------


def test_the_companion_reference_file_is_written_byte_exact_and_both_are_private(store):
    directory, _ = store
    receipt = run_install(store)
    ref = directory / "reasoning-model-credential"
    assert ref.read_bytes() == b"anthropic-api-key://owner1/ref1\n"
    assert ref.stat().st_mode & 0o777 == 0o600 and ref.stat().st_nlink == 1
    assert receipt["reference_path"] == str(ref) and receipt["profile_checked"] is False


def test_a_key_the_runtime_would_reject_is_refused_at_install(store):
    from tessera_agent.anthropic_messages import _KEY
    from tessera_agent.model_destinations import API_KEY_SHAPE

    assert _KEY is API_KEY_SHAPE
    assert run_install(store, secret=b"k" * 512)["size"] == 512
    for bad in (b"k" * 513, b"k" * 4096):
        with pytest.raises(CredentialRefused) as refused:
            run_install(store, secret=bad, replace=True)
        assert refused.value.code == "invalid_credential_shape" and "kkkk" not in str(refused.value)


def test_a_profile_credential_ref_that_differs_is_refused_before_writing(store, tmp_path):
    directory, _ = store
    profile = tmp_path / "reasoning.json"
    profile.write_text(json.dumps({"credential_ref": f"{NAME}://owner1/other"}))
    with pytest.raises(CredentialRefused) as refused:
        run_install(store, profile_path=profile)
    assert refused.value.code == "reference_mismatch"
    assert f"{NAME}://owner1/other" in str(refused.value) and SENTINEL not in str(refused.value)
    assert list(directory.iterdir()) == []
    profile.write_text(json.dumps({"credential_ref": f"{NAME}://owner1/ref1", "model": "x"}))
    assert run_install(store, profile_path=profile)["profile_checked"] is True
    missing = tmp_path / "absent.json"
    assert run_install(store, replace=True, profile_path=missing)["profile_checked"] is False


def test_the_command_line_checks_the_profile_and_needs_user_store_without_root(
    store, monkeypatch, capsys, tmp_path
):
    directory, units = store

    class Stdin:
        buffer = io.BytesIO(SECRET + b"\n")

    monkeypatch.setattr(sc.sys, "stdin", Stdin)
    base = ["install", "--service", SERVICE, "--name", NAME, "--owner", "owner1", "--reference",
            "ref1", "--credentials-dir", str(directory), "--unit-dir", str(units)]  # fmt: skip
    monkeypatch.setattr(sc.os, "geteuid", lambda: 1000)
    assert sc.main(base) == sc.EXIT_REFUSED  # --credentials-dir alone no longer drops root
    assert "needs root" in capsys.readouterr().err and list(directory.iterdir()) == []
    profile = tmp_path / "p.json"
    profile.write_text(json.dumps({"credential_ref": f"{NAME}://other/ref"}))
    assert sc.main([*base, "--user-store", "--profile", str(profile)]) == sc.EXIT_REFUSED
    assert "reference_mismatch" in capsys.readouterr().err and list(directory.iterdir()) == []


# -- root ownership, links, rollback ----------------------------------------------------------


def test_a_service_install_is_root_owned_and_refused_for_anyone_else(store, monkeypatch):
    """The root-ownership property: with require_root the files are chowned to 0:0 (both files)
    and a non-root caller is refused with nothing written. Fails if the fchown is removed."""
    directory, _ = store
    with pytest.raises(CredentialRefused) as refused:
        run_install(store, require_root=True)  # the test runs as an ordinary user
    assert refused.value.code == "store_unavailable" and list(directory.iterdir()) == []
    chowned = []
    monkeypatch.setattr(sc.os, "geteuid", lambda: 0)
    monkeypatch.setattr(sc.os, "fchown", lambda fd, uid, gid: chowned.append((uid, gid)))
    monkeypatch.setattr(sc, "_expected_owner_uid", lambda require_root: os.getuid())
    receipt = run_install(store, require_root=True)
    assert chowned == [(0, 0), (0, 0)]  # the key file and the reference file
    assert receipt["reference_path"].endswith("reasoning-model-credential")
    monkeypatch.setattr(sc, "_expected_owner_uid", lambda require_root: os.getuid() + 1)
    with pytest.raises(CredentialRefused) as refused:  # the store is not owned by "root"
        run_install(store, require_root=True, replace=True)
    assert refused.value.code == "unsafe_permissions"


def test_a_hard_linked_credential_is_refused_by_replace_and_check(store):
    directory, units = store
    run_install(store)
    os.link(directory / "reasoning-anthropic-api-key", directory / "alias")
    with pytest.raises(CredentialRefused) as refused:
        run_install(store, replace=True)
    assert refused.value.code == "unsafe_permissions"
    result = sc.check(
        SERVICE, credentials_dir=directory, unit_dirs=[units], environ={}, require_root=False
    )
    assert result["ok"] is False and "anthropic-api-key:unsafe_permissions" in result["problems"]


def test_a_backup_uses_a_random_name_and_a_stale_one_is_never_restored(store, monkeypatch):
    directory, _ = store
    run_install(store)
    stale = directory / f".reasoning-anthropic-api-key.{os.getpid()}.prev"
    stale.write_bytes(b"OLD-STALE-KEY-must-never-return\n")
    os.chmod(stale, 0o600)
    monkeypatch.setattr(sc, "_append_receipt", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    with pytest.raises(CredentialRefused):
        run_install(store, secret=b"sk-ant-SECOND-0123456789abcdef", replace=True)
    assert (directory / "reasoning-anthropic-api-key").read_bytes() == SECRET + b"\n"
    assert stale.read_bytes() == b"OLD-STALE-KEY-must-never-return\n"  # left alone, not restored
    names = {sc._Placement(directory, "f", b"").backup.name for _ in range(3)}
    assert len(names) == 3  # a fresh random token each time


# -- check ------------------------------------------------------------------------------------


def test_check_fails_when_the_unit_lacks_a_declaration_and_notes_a_missing_unit(store):
    directory, units = store
    run_install(store)
    kwargs = dict(credentials_dir=directory, unit_dirs=[units], environ={}, require_root=False)
    result = sc.check(SERVICE, **kwargs)
    assert result["ok"] is True and result["unit_declared"][NAME] is None and result["note"]
    (units / "tessera-reasoning.service").write_text("[Service]\nUser=x\n")
    result = sc.check(SERVICE, **kwargs)
    assert result["ok"] is False and result["problems"] == ["not_declared_in_unit"]
    declare(units, directory)
    assert sc.check(SERVICE, **kwargs)["ok"] is True
    assert sc.check(SERVICE, **{**kwargs, "environ": {"CLAUDE_API_KEY": ""}})["ok"] is False


def test_check_through_the_command_line_exits_78_on_a_problem(store, capsys):
    directory, units = store
    run_install(store)
    (units / "tessera-reasoning.service").write_text("[Service]\nUser=x\n")
    code = sc.main(["check", "--service", SERVICE, "--credentials-dir", str(directory),
                    "--user-store", "--unit-dir", str(units)])  # fmt: skip
    assert code == sc.EXIT_REFUSED and json.loads(capsys.readouterr().out)["ok"] is False


# -- receipts recovery ------------------------------------------------------------------------


def three_receipts(store):
    directory, _ = store
    for secret in (SECRET, b"sk-ant-SECOND-0123456789abcdef", b"sk-ant-THIRD--0123456789abcdef"):
        run_install(store, secret=secret, replace=True)
    return directory / sc.RECEIPTS_NAME


def tamper_middle(path):
    rows = path.read_text().splitlines()
    middle = json.loads(rows[1])
    middle["size"] = 999
    rows[1] = json.dumps(middle, sort_keys=True)
    path.write_text("\n".join(rows) + "\n")


def test_a_broken_receipt_chain_blocks_installs_and_verify_reports_where(store):
    path = three_receipts(store)
    assert sc.verify_receipts(path)["ok"] is True and sc.verify_receipts(path)["count"] == 3
    tamper_middle(path)
    state = sc.verify_receipts(path)
    assert state["ok"] is False and state["broken_at"] == 1 and state["count"] == 1
    with pytest.raises(CredentialRefused) as refused:
        run_install(store, replace=True)
    assert refused.value.code == "store_unavailable" and "archive-broken" in str(refused.value)


def test_archive_broken_moves_the_chain_aside_and_starts_a_new_one(store, capsys):
    directory, _ = store
    path = three_receipts(store)
    with pytest.raises(CredentialRefused) as refused:  # an intact chain is never archived
        sc.archive_broken_receipts(path)
    assert refused.value.code == "receipts_intact"
    tamper_middle(path)
    first_good = json.loads(path.read_text().splitlines()[0])["hash"]
    first = sc.archive_broken_receipts(path)
    archives = [p for p in directory.iterdir() if ".broken-" in p.name]
    assert len(archives) == 1 and archives[0].stat().st_mode & 0o777 == 0o600
    assert first["event"] == "chain_restarted" and first["previous_hash"] == "0" * 64
    assert first["archived_as"] == archives[0].name and first["count_verified"] == 1
    assert first["last_good_hash"] == first_good
    assert sc.verify_receipts(path)["ok"] is True
    again = run_install(store, replace=True)  # installs work again, chained after the restart
    assert again["previous_hash"] == first["hash"]
    where = ["--service", SERVICE, "--credentials-dir", str(directory), "--user-store"]
    assert sc.main(["receipts", "verify", *where]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert sc.main(["receipts", "archive-broken", *where]) == sc.EXIT_REFUSED  # intact now


def test_refusal_messages_are_fixed_text():
    for code, message in sc._MESSAGES.items():
        assert not re.search(r"sk-|\\x", message), code


@pytest.mark.parametrize("spelling", ["--owner", "--owner-label"])
def test_install_accepts_both_owner_spellings(spelling):
    args = sc._parser().parse_args(
        ["install", "--service", SERVICE, "--name", NAME, spelling, "o1", "--reference", "r"]
    )
    assert args.owner == "o1"


def test_install_owner_help_says_reference_owner_label():
    install_help = [
        a for a in sc._parser()._subparsers._group_actions[0].choices["install"]._actions
        if "--owner" in a.option_strings
    ][0].help  # fmt: skip
    assert install_help == "reference owner label"


def test_append_receipt_fsyncs_file_and_new_directory(tmp_path, monkeypatch):
    synced = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (synced.append(os.fstat(fd).st_mode), real(fd)))
    path = tmp_path / "receipts.jsonl"
    sc._append_receipt(path, {"event": "one"})
    import stat

    assert [stat.S_ISREG(m) for m in synced] == [True, False]  # file, then its directory
    synced.clear()
    sc._append_receipt(path, {"event": "two"})
    assert [stat.S_ISREG(m) for m in synced] == [True]  # existing file: no directory sync


def test_append_receipt_ignores_directory_fsync_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "_fsync_directory", lambda d: (_ for _ in ()).throw(OSError()))
    body = sc._append_receipt(tmp_path / "receipts.jsonl", {"event": "one"})
    assert body["event"] == "one"
