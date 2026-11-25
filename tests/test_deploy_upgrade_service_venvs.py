"""``deploy-upgrade`` reinstalls the release into each registered service venv (ADR 0161)."""

from __future__ import annotations

import json
import os

from test_deploy_upgrade import Fakes, make_plan, run, steps

from tessera_control import deploy_upgrade
from tessera_control.deploy_upgrade_plan import load_plan


def venv_plan(tmp_path, venvs):
    plan, path, state = make_plan(tmp_path, behind=False, with_config=False)
    doc = json.loads(path.read_text())
    doc["install_source"] = str(tmp_path / "src")
    for service, venv in zip(doc["services"], venvs, strict=False):
        if venv:
            service["venv"] = str(venv)
    path.write_text(json.dumps(doc))
    return load_plan(path)


def make_venv(root, name):
    py = root / name / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text("")
    py.chmod(0o755)
    return root / name


class VenvFakes(Fakes):
    def __init__(self, *, fail_pip=None, version="1.0"):
        super().__init__()
        self.fail_pip, self.version = fail_pip, version

    def runner(self, argv, timeout):
        self.calls.append(list(argv))
        if "pip" in argv:
            return (1, "x") if self.fail_pip and argv[0].startswith(self.fail_pip) else (0, "")
        if argv[1:2] == ["-c"]:
            return 0, self.version + "\n"
        return 0, "ok"


def test_venv_installed_between_stop_and_restart_with_receipt(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy_upgrade, "package_version", lambda: "1.0")
    a, b = make_venv(tmp_path, "a"), make_venv(tmp_path, "b")
    plan = venv_plan(tmp_path, [a, b])
    fakes = VenvFakes()
    doc = run(plan, fakes, execute=True)
    assert doc["result"] == "ok"
    names = [s for s, _ in steps(doc)]
    first_restart = names.index("restart:control")
    assert names.index("stop:control") < names.index(f"install-venv:{a}") < first_restart
    assert [(r["venv"], r["ok"], r["version"], r["error_class"]) for r in doc["service_venvs"]] == [
        (str(a), True, "1.0", None),
        (str(b), True, "1.0", None),
    ]
    assert doc["service_venvs"][0]["release"] == "a" * 40


def test_pip_failure_blocks_start_and_is_truthful(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy_upgrade, "package_version", lambda: "1.0")
    a, b = make_venv(tmp_path, "a"), make_venv(tmp_path, "b")
    plan = venv_plan(tmp_path, [a, b])
    fakes = VenvFakes(fail_pip=str(a))
    doc = run(plan, fakes, execute=True)
    assert doc["result"] == "failed"
    assert doc["failed_step"] == f"install-venv:{a}"
    assert [r["error_class"] for r in doc["service_venvs"]] == ["pip_failed", "not_attempted"]
    assert not any(c[:1] == ["restart-control"] for c in fakes.calls)
    assert not any(s.startswith("restart") for s, _ in steps(doc))


def test_version_mismatch_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy_upgrade, "package_version", lambda: "2.0")
    a = make_venv(tmp_path, "a")
    doc = run(venv_plan(tmp_path, [a, None]), VenvFakes(version="1.0"), execute=True)
    assert doc["service_venvs"][0]["error_class"] == "version_mismatch"
    assert doc["result"] == "failed"


def test_dry_run_lists_installs_and_runs_none(tmp_path):
    a = make_venv(tmp_path, "a")
    fakes = VenvFakes()
    plan = venv_plan(tmp_path, [a, None])
    doc = deploy_upgrade.run_upgrade(plan, execute=False, runner=fakes.runner)
    assert any(f"service venv {a}" in line for line in doc["planned"])
    assert fakes.calls == []


def test_missing_venv_python_blocks_preflight(tmp_path):
    plan = venv_plan(tmp_path, [tmp_path / "nope", None])
    fakes = VenvFakes()
    doc = run(plan, fakes, execute=True)
    assert doc["result"] == "failed" and doc["failed_step"] == "preflight"
    assert any("no executable bin/python" in b for b in doc["preflight"]["blocking"])
    assert fakes.calls == []
    assert not os.path.exists(tmp_path / "nope")


def test_no_venv_means_no_receipt_rows(tmp_path):
    plan, _p, _s = make_plan(tmp_path, behind=False, with_config=False)
    doc = run(plan, Fakes(), execute=True)
    assert doc["service_venvs"] == []
