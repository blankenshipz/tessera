from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

import pytest

from tessera_control import deploy_smoke
from tessera_control.deploy_smoke import PINNED_IMAGE, run_smoke, smoke_skipped


class FakeRunner:
    def __init__(self, inspect=(0, ""), run=None, rm=(0, "")):
        self.calls: list[list[str]] = []
        self.inspect, self.run_result, self.rm = inspect, run, rm

    def __call__(self, argv, timeout):
        self.calls.append(list(argv))
        verb = argv[1] if argv[1] != "image" else "inspect"
        if verb == "inspect":
            return self._resolve(self.inspect, argv)
        if verb == "run":
            nonce = re.search(r"print\('(\w+)'\)", argv[-1]).group(1)
            outcome = self.run_result if self.run_result is not None else (0, nonce + "\n")
            return self._resolve(outcome, argv)
        return self._resolve(self.rm, argv)

    @staticmethod
    def _resolve(outcome, argv):
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def verbs(self):
        return [c[1] if c[1] != "image" else "inspect" for c in self.calls]


def test_constant_matches_check_script():
    source = Path(__file__).resolve().parents[1].joinpath("scripts/check_cpu_runtime.py")
    tree = ast.parse(source.read_text())
    values = [
        ast.literal_eval(n.value)
        for n in tree.body
        if isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "IMAGE" for t in n.targets)
    ]
    assert values == [PINNED_IMAGE]


def test_skipped():
    finding = smoke_skipped()
    assert (finding.check_id, finding.status) == ("smoke.cpu", "skipped")
    assert finding.summary == "smoke not requested; pass --smoke"


def test_success_argv_and_exact_nonce():
    runner = FakeRunner()
    finding = run_smoke(runner)
    assert finding.status == "ok" and finding.area == "smoke"
    run_argv = runner.calls[1]
    for flag in ("--rm", "--read-only"):
        assert flag in run_argv
    joined = " ".join(run_argv)
    assert "--network none" in joined
    assert "--cap-drop ALL" in joined
    assert "--security-opt no-new-privileges" in joined
    assert run_argv[run_argv.index("--name") + 1].startswith("tessera-deploy-smoke-")
    assert PINNED_IMAGE in run_argv
    assert all("pull" not in c for c in runner.calls)
    assert finding.detail["image"] == PINNED_IMAGE
    assert finding.detail["cleanup"] is True
    assert "duration_seconds" in finding.detail


@pytest.mark.parametrize("output", ["", "wrong\n", "prefix deadbeef"])
def test_nonce_mismatch_fails(output):
    finding = run_smoke(FakeRunner(run=(0, output)))
    assert finding.status == "fail"


def test_nonce_must_be_exact():
    class Echo(FakeRunner):
        def __call__(self, argv, timeout):
            if len(argv) > 1 and argv[1] == "run":
                self.calls.append(list(argv))
                nonce = re.search(r"print\('(\w+)'\)", argv[-1]).group(1)
                return 0, nonce + "extra\n"
            return super().__call__(argv, timeout)

    assert run_smoke(Echo()).status == "fail"


def test_missing_image_fails_with_manual_pull_fix():
    runner = FakeRunner(inspect=(1, "No such image"))
    finding = run_smoke(runner)
    assert finding.status == "fail"
    assert "already exist locally" in finding.summary and "never pull" in finding.summary
    assert finding.fix is not None and finding.fix.automatic is False
    assert finding.fix.command == f"docker pull {PINNED_IMAGE}"
    assert runner.verbs() == ["inspect"]


def test_missing_docker_fails_with_manual_pull_fix():
    finding = run_smoke(FakeRunner(inspect=FileNotFoundError("docker")))
    assert finding.status == "fail"
    assert finding.fix is not None and not finding.fix.automatic


def test_nonzero_exit_fails_and_cleans_up():
    runner = FakeRunner(run=(125, "boom " * 100))
    finding = run_smoke(runner)
    assert finding.status == "fail"
    assert finding.detail["exit_code"] == 125
    assert len(finding.detail["output"]) == 200
    assert runner.verbs() == ["inspect", "run", "rm"]


def test_timeout_fails_and_cleanup_targets_exact_name():
    runner = FakeRunner(run=subprocess.TimeoutExpired(["docker"], 1))
    finding = run_smoke(runner)
    assert finding.status == "fail"
    name = runner.calls[1][runner.calls[1].index("--name") + 1]
    assert runner.calls[2] == ["docker", "rm", "-f", name]


def test_cleanup_failure_reported_result_unchanged():
    ok = run_smoke(FakeRunner(rm=(1, "nope")))
    assert ok.status == "ok" and ok.detail["cleanup"] is False
    raised = run_smoke(FakeRunner(rm=subprocess.TimeoutExpired(["docker"], 1)))
    assert raised.status == "ok" and raised.detail["cleanup"] is False
    bad = run_smoke(FakeRunner(run=(1, "x"), rm=(1, "nope")))
    assert bad.status == "fail" and bad.detail["cleanup"] is False


def test_default_runner_minimal_env_and_truncation(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs, argv=argv)
        return subprocess.CompletedProcess(argv, 3, b"a" * 5000, b"b" * 5000)

    monkeypatch.setenv("SECRET_TOKEN", "hunter2")
    monkeypatch.setattr(deploy_smoke.subprocess, "run", fake_run)
    code, output = deploy_smoke.default_runner(["docker", "ps"], 7)
    assert code == 3 and len(output.encode()) == 4096
    assert set(seen["env"]) == {"PATH"}
    assert seen["shell"] is False and seen["timeout"] == 7
    assert seen["argv"] == ["docker", "ps"]
