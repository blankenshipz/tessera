"""The executor's ``source_revision`` is refused at load unless it is the pin the live check runs.

A config whose pin the live check would refuse used to load, lease a grant and stop every route
at the live-check start with no receipt. The loader now uses the live check's own shape check
and installed-source measurement, and the deploy check can refresh the pin in place.
"""

from __future__ import annotations

import json
import os
import stat

import pytest
from test_deploy_config import executor_document, write

from tessera_control import deploy_config, deploy_upgrade
from tessera_control import salad_live_check as live
from tessera_control.campaign_salad_executor import ExecutorConfig, load_config
from tessera_control.cli import main

COMMIT = "a" * 40
SHAPE_REFUSED = {
    "missing pin": COMMIT,
    "short commit": "a" * 39 + "+sha256:" + "b" * 64,
    "long commit": "a" * 41 + "+sha256:" + "b" * 64,
    "missing +sha256:": COMMIT + "b" * 64,
    "other separator": COMMIT + "+sha512:" + "b" * 64,
    "short digest": COMMIT + "+sha256:" + "b" * 63,
    "non-hex digest": COMMIT + "+sha256:" + "g" * 64,
    "upper-case digest": COMMIT + "+sha256:" + "B" * 64,
    "trailing newline": COMMIT + "+sha256:" + "b" * 64 + "\n",
}


def _tree(root, files=("tessera_control/a.py", "tessera_worker/b.py")):
    for name in files:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {name}\n")
    return root


def _config(tmp_path, source_revision, mode=0o600):
    base = tmp_path / "executor"
    base.mkdir(mode=0o700, exist_ok=True)
    return write(base, dict(executor_document(base), source_revision=source_revision), mode=mode)


@pytest.fixture
def receipts(tmp_path):
    directory = tmp_path / "receipts"
    directory.mkdir(mode=0o700)
    return directory


# -- refused at load ------------------------------------------------------------------------------


@pytest.mark.parametrize("value", SHAPE_REFUSED.values(), ids=SHAPE_REFUSED.keys())
def test_a_pin_the_live_check_refuses_is_refused_at_load(tmp_path, value):
    assert not live.is_source_pin(value)
    base = executor_document(tmp_path)
    with pytest.raises(ValueError, match="source_revision"):
        ExecutorConfig.model_validate(dict(base, source_revision=value))
    with pytest.raises(ValueError, match=r"source_revision must be '<40 lowercase hex") as error:
        load_config(_config(tmp_path, value))
    assert value.strip() not in str(error.value)


def test_a_non_string_pin_is_refused_with_the_pin_message(tmp_path):
    with pytest.raises(ValueError, match="source_revision must be"):
        load_config(_config(tmp_path, 1234))


def test_a_well_formed_pin_of_another_source_is_refused_at_load(tmp_path):
    stale = COMMIT + "+sha256:" + "b" * 64
    assert live.is_source_pin(stale)  # the shape passes; the installed digest does not
    with pytest.raises(ValueError, match="does not pin the installed source"):
        load_config(_config(tmp_path, stale))


def test_the_installed_pin_loads(tmp_path):
    pin = live.installed_source_pin(COMMIT)
    assert load_config(_config(tmp_path, pin)).source_revision == pin


def test_other_config_errors_keep_the_generic_message(tmp_path):
    path = _config(tmp_path, live.installed_source_pin(COMMIT))
    document = json.loads(path.read_text())
    path.write_text(json.dumps(dict(document, poll_seconds=1)))
    with pytest.raises(ValueError, match="owner-only absolute"):
        load_config(path)


# -- the pin is measured from the installed source -----------------------------------------------


def test_the_installed_pin_is_the_shape_and_digest_run_checks(tmp_path):
    root = _tree(tmp_path / "site")
    pin = live.installed_source_pin(COMMIT, root)
    assert live.is_source_pin(pin)
    assert pin == f"{COMMIT}+sha256:{live.source_digest(root)}"
    # The default root is the one ``run`` measures.
    assert live.installed_source_pin(COMMIT).split("+sha256:")[1] == live.source_digest()
    (root / "tessera_worker" / "b.py").write_text("changed\n")
    assert live.installed_source_pin(COMMIT, root) != pin


@pytest.mark.parametrize("case", ["empty", "symlink", "missing"])
def test_an_unmeasurable_source_is_refused(tmp_path, case):
    root = tmp_path / "site"
    if case == "empty":
        (root / "tessera_control").mkdir(parents=True)
    elif case == "symlink":
        _tree(root)
        (root / "tessera_control" / "link.py").symlink_to(root / "tessera_worker" / "b.py")
    with pytest.raises(ValueError, match="cannot be measured"):
        live.installed_source_pin(COMMIT, root)


@pytest.mark.parametrize("commit", ["a" * 39, "A" * 40, "a" * 40 + "\n", None])
def test_a_bad_commit_is_refused(commit):
    with pytest.raises(ValueError, match="40 lowercase hex"):
        live.installed_source_pin(commit)


# -- the deploy fix rewrites the config in place --------------------------------------------------


def test_refresh_rewrites_the_pin_atomically_and_keeps_the_mode(tmp_path, receipts):
    path = _config(tmp_path, COMMIT, mode=0o400)
    before = json.loads(path.read_text())
    inode = path.stat().st_ino
    (finding,) = [
        f for f in deploy_config.check_config(path, label="1") if f.check_id.endswith("source_pin")
    ]
    assert finding.status == "fail" and finding.fix.fix_id == "refresh_source_pin"
    receipt = deploy_config.apply_refresh_source_pin(path, receipts, "c" * 40)
    pin = live.installed_source_pin("c" * 40)
    after = json.loads(path.read_text())
    assert after == dict(before, source_revision=pin)
    assert stat.S_IMODE(path.stat().st_mode) == 0o400
    assert path.stat().st_ino != inode  # replaced, not written in place
    assert not [name for name in os.listdir(path.parent) if name.startswith(".deploy-check-")]
    assert receipt["fix"] == "refresh_source_pin" and receipt["source_revision"] == pin
    assert str(tmp_path) not in json.dumps(receipt)
    path.chmod(0o600)  # the executor loader itself accepts exactly 0600
    assert load_config(path).source_revision == pin
    # Already pinned: nothing to do, no second receipt.
    assert deploy_config.apply_refresh_source_pin(path, receipts, "c" * 40) is None
    assert len(list(receipts.glob("deploy-fix-*.json"))) == 1


def test_refresh_pins_to_the_measured_root(tmp_path, receipts):
    root = _tree(tmp_path / "site")
    path = _config(tmp_path, COMMIT)
    deploy_config.apply_refresh_source_pin(path, receipts, COMMIT, root=root)
    assert json.loads(path.read_text())["source_revision"] == live.installed_source_pin(
        COMMIT, root
    )


def test_refresh_refuses_an_unmeasurable_source_and_changes_nothing(tmp_path, receipts):
    path = _config(tmp_path, COMMIT)
    before = path.read_bytes()
    (tmp_path / "empty").mkdir()
    with pytest.raises(ValueError, match="cannot be measured"):
        deploy_config.apply_refresh_source_pin(path, receipts, COMMIT, root=tmp_path / "empty")
    assert path.read_bytes() == before and list(receipts.iterdir()) == []


def test_the_command_refreshes_the_pin_and_needs_a_commit(tmp_path, receipts, capsys, monkeypatch):
    path = _config(tmp_path, COMMIT)
    monkeypatch.chdir(tmp_path)  # no checkout here: the commit is not compared to a head
    argv = ["tessera", "deploy-check", "--format", "json", "--executor-config", str(path)]
    argv += ["--apply", "refresh_source_pin", "--receipt-dir", str(receipts)]
    monkeypatch.setattr("sys.argv", argv)
    assert main() == 2
    assert "--source-commit" in capsys.readouterr().err
    assert json.loads(path.read_text())["source_revision"] == COMMIT
    monkeypatch.setattr("sys.argv", [*argv, "--source-commit", COMMIT])
    main()
    report = json.loads(capsys.readouterr().out)
    checks = {c["id"]: c["status"] for c in report["checks"]}
    assert checks["config.1.source_pin"] == "ok"
    assert [entry["fix"] for entry in report["applied"]] == ["refresh_source_pin"]
    assert load_config(path).source_revision == live.installed_source_pin(COMMIT)


def test_deploy_upgrade_refreshes_a_stale_pin_only_with_the_plan_commit(tmp_path):
    path = _config(tmp_path, COMMIT + "+sha256:" + "b" * 64)

    class Plan:
        state_dir = str(tmp_path / "state")
        executor_configs = [str(path)]
        live_configs: list[str] = []
        providers_file = None
        max_backup_bytes = None
        source_revision = COMMIT

    (tmp_path / "state").mkdir(mode=0o700)
    _findings, blocking = deploy_upgrade.preflight(Plan, smoke=False)
    assert "config.1.source_pin" not in blocking
    Plan.source_revision = None
    _findings, blocking = deploy_upgrade.preflight(Plan, smoke=False)
    assert "config.1.source_pin" in blocking


# -- the live check refuses what it refused before ------------------------------------------------


def test_run_still_refuses_every_pin_it_refused(tmp_path, monkeypatch):
    from test_salad_live_check import concrete_session

    session, h, _ = concrete_session(tmp_path, monkeypatch)
    receipts = tmp_path / "live-receipts"
    receipts.mkdir(mode=0o700)
    try:
        for value in SHAPE_REFUSED.values():
            with pytest.raises(ValueError, match="bounded source-pinned run required"):
                live.run(session, route_id=h.route["id"], source_revision=value,
                         receipt_directory=receipts)  # fmt: skip
        stale = COMMIT + "+sha256:" + "b" * 64
        with pytest.raises(ValueError, match="source inventory changed"):
            live.run(session, route_id=h.route["id"], source_revision=stale,
                     receipt_directory=receipts)  # fmt: skip
        assert os.listdir(receipts) == []  # refused before the started receipt
    finally:
        session.close()
        h.server.resolver.close()
