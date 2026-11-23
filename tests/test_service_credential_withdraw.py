"""Withdrawing a service credential: the same two files ``install`` writes are removed (both or
neither) through the checked credential directory, a chained receipt records who, which reference
and when, and the key file is never opened. Real files in a temporary directory; no root needed."""

from __future__ import annotations

import builtins
import io
import json
import os
from pathlib import Path

import pytest
from test_reasoning_credential_references import REF_LABEL, anthropic  # noqa: F401
from test_service_credentials import (  # noqa: F401
    NAME,
    SECRET,
    SENTINEL,
    SERVICE,
    run_install,
    store,
    tamper_middle,
)

from tessera_agent import first_call_readiness as fcr
from tessera_agent import service_credentials as sc
from tessera_agent.service_credentials import CredentialRefused

KEY, REFERENCE = "reasoning-anthropic-api-key", "reasoning-model-credential"


def run_withdraw(store, **over):  # noqa: F811 - the imported fixture's value
    directory, _ = store
    options = dict(credentials_dir=directory, owner_label="operator1", require_root=False)
    options.update(over)
    return sc.withdraw(SERVICE, NAME, **options)


def receipts(directory: Path) -> list[dict]:
    return sc.read_receipts(directory / sc.RECEIPTS_NAME)


def assert_no_secret(text: str) -> None:
    assert SENTINEL not in text
    assert not any(SENTINEL[i : i + 8] in text for i in range(len(SENTINEL) - 7))


def forbid_opening_the_key(monkeypatch):
    """From now on, fail any open of the key file or a backup of it (os.open, open, io.open,
    Path.open/read_bytes/read_text, _read_private)."""

    def is_key(path) -> bool:
        try:
            return os.fspath(path).endswith(KEY) or os.fspath(path).endswith(".prev")
        except TypeError:
            return False

    def guard(real):
        def guarded(first, *args, **kwargs):
            assert not is_key(first), "the key file was opened"
            return real(first, *args, **kwargs)

        return guarded

    monkeypatch.setattr(os, "open", guard(os.open))
    monkeypatch.setattr(builtins, "open", guard(builtins.open))
    monkeypatch.setattr(io, "open", guard(io.open))
    for method in ("open", "read_bytes", "read_text"):
        monkeypatch.setattr(Path, method, guard(getattr(Path, method)))
    monkeypatch.setattr(sc, "_read_private", guard(sc._read_private))


def test_withdraw_removes_both_files_and_chains_a_receipt_from_the_install(
    store,  # noqa: F811
    monkeypatch,
):
    directory, _ = store
    installed = run_install(store)
    forbid_opening_the_key(monkeypatch)
    receipt = run_withdraw(store, reason_label="rollback")
    assert not os.path.lexists(directory / KEY) and not os.path.lexists(directory / REFERENCE)
    assert receipt["action"] == "withdraw" and receipt["outcome"] == "removed"
    assert receipt["removed_paths"] == [str(directory / KEY), str(directory / REFERENCE)]
    assert receipt["reference"] == installed["reference"] == f"{NAME}://owner1/ref1"
    assert receipt["fingerprint"] == installed["fingerprint"]
    assert receipt["install_receipt_hash"] == installed["hash"]
    assert receipt["previous_hash"] == installed["hash"]
    assert receipt["withdrawn_by_uid"] == os.getuid()
    assert receipt["owner_label"] == "operator1" and receipt["reason_label"] == "rollback"
    assert receipt["service"] == SERVICE and receipt["credential"] == NAME and receipt["at"]
    assert sc.verify_receipts(directory / sc.RECEIPTS_NAME) == {
        "ok": True,
        "count": 2,
        "broken_at": None,
        "last_good_hash": receipt["hash"],
    }
    # no backup, temp or other file is left behind; only the receipt chain remains
    monkeypatch.undo()
    assert sorted(p.name for p in directory.iterdir()) == [sc.RECEIPTS_NAME]


def test_a_second_withdraw_records_already_absent_and_exits_zero(store, capsys):  # noqa: F811
    directory, _ = store
    run_install(store)
    first = run_withdraw(store)
    code = sc.main(
        ["withdraw", "--service", SERVICE, "--name", NAME, "--owner-label", "operator1",
         "--reason", "again", "--credentials-dir", str(directory), "--user-store"]
    )  # fmt: skip
    out = capsys.readouterr()
    assert code == 0 and out.err == ""
    second = json.loads(out.out)
    assert second["outcome"] == "already_absent" and second["removed_paths"] == []
    assert second["previous_hash"] == first["hash"]
    assert second["reference"] == first["reference"]  # still the latest install's reference
    assert second == receipts(directory)[-1]


def test_withdraw_with_no_install_ever_records_already_absent_with_null_reference(store):  # noqa: F811
    receipt = run_withdraw(store)
    assert receipt["outcome"] == "already_absent"
    assert receipt["reference"] is None and receipt["fingerprint"] is None
    assert receipt["install_receipt_hash"] is None and receipt["previous_hash"] == "0" * 64


def test_only_the_present_file_is_removed(store):  # noqa: F811
    directory, _ = store
    run_install(store)
    (directory / REFERENCE).unlink()
    receipt = run_withdraw(store)
    assert receipt["outcome"] == "removed" and receipt["removed_paths"] == [str(directory / KEY)]
    assert not os.path.lexists(directory / KEY)


@pytest.mark.parametrize("name", [KEY, REFERENCE])
def test_a_symlink_at_either_name_is_refused_and_nothing_is_removed(store, tmp_path, name):  # noqa: F811
    directory, _ = store
    run_install(store)
    outside = tmp_path / "outside"
    outside.write_text("not a credential\n")
    os.unlink(directory / name)
    os.symlink(outside, directory / name)
    before = sorted(p.name for p in directory.iterdir())
    count = len(receipts(directory))
    with pytest.raises(CredentialRefused) as refused:
        run_withdraw(store)
    assert refused.value.code == "unsafe_permissions"
    assert sorted(p.name for p in directory.iterdir()) == before
    assert os.path.islink(directory / name) and outside.read_text() == "not a credential\n"
    other = REFERENCE if name == KEY else KEY
    assert os.path.isfile(directory / other) and len(receipts(directory)) == count


def test_a_hard_linked_file_is_refused_and_nothing_is_removed(store):  # noqa: F811
    directory, _ = store
    run_install(store)
    os.link(directory / REFERENCE, directory / "alias")
    with pytest.raises(CredentialRefused) as refused:
        run_withdraw(store)
    assert refused.value.code == "unsafe_permissions"
    assert (directory / KEY).is_file() and (directory / REFERENCE).is_file()


def test_a_directory_at_a_name_and_an_unsafe_store_are_refused(store):  # noqa: F811
    directory, _ = store
    (directory / REFERENCE).mkdir()
    with pytest.raises(CredentialRefused) as refused:
        run_withdraw(store)
    assert refused.value.code == "unsafe_permissions"
    (directory / REFERENCE).rmdir()
    os.chmod(directory, 0o777)
    with pytest.raises(CredentialRefused) as refused:
        run_withdraw(store)
    assert refused.value.code == "unsafe_permissions"
    os.chmod(directory, 0o755)
    with pytest.raises(CredentialRefused) as refused:
        run_withdraw(store, credentials_dir="relative/dir")
    assert refused.value.code == "unsafe_permissions"


def test_a_failure_removing_the_second_file_restores_the_first(store, monkeypatch):  # noqa: F811
    directory, _ = store
    run_install(store)
    key_inode = (directory / KEY).stat().st_ino
    count = len(receipts(directory))
    real_unlink = os.unlink

    def failing_unlink(path, *args, **kwargs):
        if os.fspath(path) == str(directory / REFERENCE):
            raise PermissionError("synthetic")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(sc.os, "unlink", failing_unlink)
    with pytest.raises(CredentialRefused) as refused:
        run_withdraw(store)
    assert refused.value.code == "store_unavailable"
    monkeypatch.undo()
    info = (directory / KEY).stat()
    assert info.st_ino == key_inode and info.st_nlink == 1  # the very same file, put back
    assert (directory / REFERENCE).is_file() and (directory / REFERENCE).stat().st_nlink == 1
    assert [p.name for p in directory.iterdir() if p.name.startswith(".")] == []
    assert len(receipts(directory)) == count  # no receipt claims a removal that did not happen


def test_a_failed_receipt_restores_both_files(store, monkeypatch):  # noqa: F811
    directory, _ = store
    run_install(store)
    monkeypatch.setattr(sc, "_append_receipt", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    with pytest.raises(CredentialRefused) as refused:
        run_withdraw(store)
    assert refused.value.code == "store_unavailable"
    assert (directory / KEY).stat().st_nlink == 1 and (directory / REFERENCE).is_file()
    assert [p.name for p in directory.iterdir() if p.name.startswith(".")] == []


def test_a_broken_chain_refuses_before_touching_files(store, monkeypatch):  # noqa: F811
    directory, _ = store
    for secret in (SECRET, b"sk-ant-SECOND-0123456789abcdef", b"sk-ant-THIRD--0123456789abcdef"):
        run_install(store, secret=secret, replace=True)
    tamper_middle(directory / sc.RECEIPTS_NAME)

    def untouched(*args, **kwargs):
        raise AssertionError("a file was touched")

    monkeypatch.setattr(sc.os, "link", untouched)
    with pytest.raises(CredentialRefused) as refused:
        run_withdraw(store)
    assert refused.value.code == "store_unavailable" and "archive-broken" in str(refused.value)
    monkeypatch.undo()
    assert (directory / KEY).is_file() and (directory / REFERENCE).is_file()


def test_a_service_withdraw_needs_root_and_labels_are_checked(store, monkeypatch):  # noqa: F811
    monkeypatch.setattr(sc.os, "geteuid", lambda: 1000)
    with pytest.raises(CredentialRefused) as refused:
        run_withdraw(store, require_root=True)
    assert refused.value.code == "store_unavailable"
    with pytest.raises(CredentialRefused) as refused:
        run_withdraw(store, owner_label="has space")
    assert refused.value.code == "invalid_label"
    with pytest.raises(CredentialRefused) as refused:
        run_withdraw(store, reason_label="../x")
    assert refused.value.code == "invalid_label"
    with pytest.raises(CredentialRefused) as refused:
        sc.withdraw(SERVICE, "other", owner_label="o", require_root=False)
    assert refused.value.code == "unknown_credential"


def test_the_command_line_prints_only_the_receipt_and_no_secret(store, capsys):  # noqa: F811
    directory, units = store
    run_install(store)
    capsys.readouterr()
    where = ["--service", SERVICE, "--credentials-dir", str(directory), "--user-store"]
    code = sc.main(["withdraw", *where, "--name", NAME, "--owner-label", "operator1"])
    out = capsys.readouterr()
    assert code == 0 and out.err == ""
    receipt = json.loads(out.out)
    assert receipt["outcome"] == "removed" and receipt["reason_label"] is None
    assert_no_secret(out.out + out.err + (directory / sc.RECEIPTS_NAME).read_text())
    # a refusal exits like install's, with a sanitized message on stderr only
    os.symlink(directory / sc.RECEIPTS_NAME, directory / KEY)
    code = sc.main(["withdraw", *where, "--name", NAME, "--owner-label", "operator1"])
    out = capsys.readouterr()
    assert code == sc.EXIT_REFUSED and out.out == ""
    assert json.loads(out.err)["refused"] == "unsafe_permissions"
    assert_no_secret(out.err)


def test_check_and_list_report_not_configured_after_withdraw(store, capsys):  # noqa: F811
    directory, units = store
    run_install(store)
    kwargs = dict(credentials_dir=directory, unit_dirs=[units], environ={}, require_root=False)
    assert sc.check(SERVICE, **kwargs)["ok"] is True
    run_withdraw(store)
    result = sc.check(SERVICE, **kwargs)
    assert result["ok"] is False and result["credentials"] == []
    assert result["problems"] == [f"{NAME}:missing_credential"]
    where = ["--service", SERVICE, "--credentials-dir", str(directory), "--user-store"]
    capsys.readouterr()
    assert sc.main(["list", *where]) == 0
    assert json.loads(capsys.readouterr().out)["installed"] == []


def test_first_call_readiness_reports_the_credential_missing_after_withdraw(
    anthropic,  # noqa: F811
    tmp_path,
):
    directory, profile, _ = anthropic  # a private store with a profile naming owner1/REF_LABEL
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    sc.install(SERVICE, NAME, io.BytesIO(SECRET), credentials_dir=directory, unit_dirs=[],
               environ={}, owner_label="owner1", reference_label=REF_LABEL,
               require_root=False)  # fmt: skip
    files = {"model-credential": REFERENCE, "anthropic-api-key": KEY}  # the stored file names

    def report():
        return fcr.run_checks(
            profile_path=directory / "profile",
            credentials_directory=directory,
            state_dir=state,
            environment={},
            credential_files=files,
        )

    def status(result, name):
        return next(c for c in result["checks"] if c["name"] == name)["status"]

    before = report()
    assert status(before, "credential_reference") == "ok" and status(before, "api_key_file") == "ok"
    sc.withdraw(SERVICE, NAME, credentials_dir=directory, owner_label="operator1",
                require_root=False)  # fmt: skip
    after = report()
    assert status(after, "credential_reference") == "missing"
    assert status(after, "api_key_file") == "missing"
    assert after["key"] == {"present": False, "size_ok": None}
    assert after["overall"] == "not_ready"  # the reference is gone too, so not just the key
    assert_no_secret(json.dumps(after))


def test_withdraw_accepts_both_owner_spellings_and_help_text():
    for spelling in ("--owner", "--owner-label"):
        args = sc._parser().parse_args(
            ["withdraw", "--service", SERVICE, "--name", NAME, spelling, "o1"]
        )
        assert args.owner_label == "o1"
    action = [
        a for a in sc._parser()._subparsers._group_actions[0].choices["withdraw"]._actions
        if "--owner-label" in a.option_strings
    ][0]  # fmt: skip
    assert action.help == "reference owner label"


def test_withdraw_cli_with_owner_alias(store, capsys):  # noqa: F811
    directory, _ = store
    code = sc.main(
        ["withdraw", "--service", SERVICE, "--name", NAME, "--owner", "operator1",
         "--credentials-dir", str(directory), "--user-store"]
    )  # fmt: skip
    assert code == 0
    assert json.loads(capsys.readouterr().out)["owner_label"] == "operator1"
