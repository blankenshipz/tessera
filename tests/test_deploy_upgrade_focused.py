"""``tessera deploy-upgrade`` with focused tests in the preflight.

The plan's range selects the focused tests; a dry run only lists them, an execute run runs them
before any service is stopped and a failure blocks the upgrade. Git and pytest are faked.
"""

from __future__ import annotations

import copy
import json
import sys

import pytest
from test_deploy_upgrade import Fakes, make_plan, steps

from tessera_control import deploy_upgrade, focused_tests
from tessera_control.deploy_upgrade_plan import PLAN_SCHEMA, PlanError, UpgradePlan, load_plan
from tessera_control.store import DATABASE_NAME

FROM = "b" * 40
TO = "a" * 40  # make_plan's source_revision


class Recorder:
    """The focused runner and the service runner share one log, so the order is visible."""

    def __init__(self, fakes: Fakes, exit_status: int = 0):
        self.log: list[str] = []
        self.runs: list[tuple] = []
        self.exit_status = exit_status
        self.fakes = fakes

    def focused(self, files, root, jobs):
        self.log.append("focused")
        self.runs.append((list(files), root, jobs))
        return self.exit_status

    def service(self, argv, timeout):
        self.log.append(argv[0])
        return self.fakes.runner(argv, timeout)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A fake checkout at TO; git reports ``changed`` for the plan's range."""
    root = tmp_path / "repo"
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "tests").mkdir()
    for name in ("test_alpha.py", "test_beta.py", "test_smoke.py"):
        (root / "tests" / name).write_text("def test_x():\n    pass\n")
    state = {"root": root, "changed": ["tests/test_alpha.py"], "ranges": [], "head": TO}

    def changed_files(where, range_spec=None, base="main", *, exact=False):
        state["ranges"].append((range_spec, exact))
        return list(state["changed"])

    monkeypatch.setattr(focused_tests, "changed_files", changed_files)
    monkeypatch.setattr(deploy_upgrade, "checkout_head", lambda where: state["head"])
    return state


def focused_plan(tmp_path, root, **extra):
    plan, path, state = make_plan(tmp_path)
    document = json.loads(path.read_text())
    document.update(from_revision=FROM, focused_tests=True, repo_root=str(root), **extra)
    path.write_text(json.dumps(document))
    return load_plan(path), state


def upgrade(plan, recorder, *, execute):
    return deploy_upgrade.run_upgrade(
        plan,
        execute=execute,
        runner=recorder.service,
        probe=recorder.fakes.probe,
        sleep=lambda _s: None,
        focused_runner=recorder.focused,
    )


def focused_check(document):
    return next(c for c in document["preflight"]["checks"] if c["id"] == "tests.focused")


# -- plan ---------------------------------------------------------------------------------


def _plan_document(**extra) -> dict:
    return {
        "schema": PLAN_SCHEMA,
        "state_dir": "/srv/state",
        "backup_dir": "/srv/backups",
        "receipt_dir": "/srv/receipts",
        "services": [
            {
                "name": "api",
                "restart_argv": ["restart-api"],
                "health": {"kind": "http", "url": "http://127.0.0.1:8080/health"},
            }
        ],
        **extra,
    }


def test_an_existing_plan_keeps_its_defaults():
    plan = UpgradePlan.model_validate(_plan_document())
    assert plan.focused_tests is False and plan.from_revision is None
    assert plan.focused_tests_jobs == 2 and plan.repo_root is None


def test_a_focused_plan_needs_both_revisions():
    good = _plan_document(source_revision=TO, from_revision=FROM, focused_tests=True)
    plan = UpgradePlan.model_validate(good)
    assert plan.focused_tests and plan.from_revision == FROM
    for missing in ("source_revision", "from_revision"):
        bad = copy.deepcopy(good)
        del bad[missing]
        with pytest.raises(ValueError, match="focused_tests needs"):
            UpgradePlan.model_validate(bad)
    # Revisions alone (focused tests off) are fine.
    UpgradePlan.model_validate(_plan_document(from_revision=FROM))


@pytest.mark.parametrize(
    "extra",
    [
        {"from_revision": "B" * 40},
        {"from_revision": "b" * 39},
        {"focused_tests_jobs": 0},
        {"focused_tests_jobs": 5},
        {"focused_tests_jobs": "2"},
        {"focused_tests": "yes"},
        {"repo_root": "relative/repo"},
        {"repo_root": "/srv/../repo"},
    ],
)
def test_bad_focused_fields_are_refused(extra):
    with pytest.raises(ValueError):
        UpgradePlan.model_validate(_plan_document(source_revision=TO, **extra))


def test_a_plan_file_without_the_range_is_refused_naming_no_value(tmp_path):
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(_plan_document(focused_tests=True, source_revision=TO)))
    path.chmod(0o600)
    with pytest.raises(PlanError, match="invalid at") as refused:
        load_plan(path)
    assert TO not in str(refused.value)


# -- dry run --------------------------------------------------------------------------------


def test_a_dry_run_lists_the_selection_and_runs_nothing(tmp_path, repo):
    plan, _state = focused_plan(tmp_path, repo["root"])
    recorder = Recorder(Fakes())
    document = upgrade(plan, recorder, execute=False)
    assert document["result"] == "ok" and document["mode"] == "dry-run"
    assert recorder.log == [] and recorder.runs == []
    assert repo["ranges"] == [(f"{FROM}..{TO}", True)]
    check = focused_check(document)
    assert check["status"] == "ok" and "1 focused test file(s) selected" in check["summary"]
    assert "changed 1" in check["summary"]
    entry = document["focused_tests"]
    assert entry["result"] == "listed" and entry["files"] == ["tests/test_alpha.py"]
    assert entry["selected"] == 1 and entry["jobs"] == 2
    assert entry["from_revision"] == FROM and entry["to_revision"] == TO
    assert document["planned"][0].startswith("run the focused tests")
    assert list((tmp_path / "receipts").iterdir()) == []


def test_a_dry_run_reports_an_unmappable_range_as_blocking(tmp_path, repo):
    repo["changed"] = ["Makefile"]
    plan, _state = focused_plan(tmp_path, repo["root"])
    recorder = Recorder(Fakes())
    document = upgrade(plan, recorder, execute=False)
    assert document["result"] == "failed" and "tests.focused" in document["preflight"]["blocking"]
    assert document["focused_tests"]["result"] == "unmapped"
    assert document["focused_tests"]["unmapped"] == ["Makefile"]
    assert recorder.runs == []


# -- execute --------------------------------------------------------------------------------


def test_execute_runs_the_focused_tests_before_any_service_stops(tmp_path, repo):
    (repo["root"] / "dev").mkdir()
    (repo["root"] / focused_tests.OVERRIDES_FILE).write_text(
        '[smoke]\nfiles = ["tests/test_smoke.py"]\n'
    )
    plan, _state = focused_plan(tmp_path, repo["root"], focused_tests_jobs=3)
    recorder = Recorder(Fakes())
    document = upgrade(plan, recorder, execute=True)
    assert document["result"] == "ok", document["steps"]
    assert recorder.log[0] == "focused" and recorder.log[1].startswith("stop-")
    files = ["tests/test_alpha.py", "tests/test_smoke.py"]
    assert recorder.runs == [(files, repo["root"], 3)]
    entry = document["focused_tests"]
    assert entry["result"] == "passed" and entry["files"] == files and entry["selected"] == 2
    assert entry["smoke"] == ["tests/test_smoke.py"] and entry["jobs"] == 3
    assert entry["duration_seconds"] >= 0
    assert focused_check(document)["status"] == "ok"
    assert steps(document)[0] == ("preflight", True)


def test_a_failing_selection_blocks_before_stop_backup_or_migrate(tmp_path, repo):
    plan, state = focused_plan(tmp_path, repo["root"])
    before = (state / DATABASE_NAME).read_bytes()
    fakes = Fakes()
    recorder = Recorder(fakes, exit_status=1)
    document = upgrade(plan, recorder, execute=True)
    assert document["result"] == "failed" and document["failed_step"] == "preflight"
    assert document["preflight"]["blocking"] == ["tests.focused"]
    assert recorder.log == ["focused"] and fakes.calls == []  # nothing stopped or restarted
    assert [s["step"] for s in document["steps"]] == ["preflight"]
    assert document["backup"] is None and document["fixes"] == []
    assert list((tmp_path / "backups").iterdir()) == []
    assert (state / DATABASE_NAME).read_bytes() == before  # not migrated
    assert document["focused_tests"]["result"] == "failed"
    assert focused_check(document)["status"] == "fail"


def test_an_unmappable_range_blocks_and_never_runs_the_full_suite(tmp_path, repo, monkeypatch):
    repo["changed"] = ["Makefile"]
    monkeypatch.setattr(focused_tests, "run_full", pytest.fail)
    plan, _state = focused_plan(tmp_path, repo["root"])
    fakes = Fakes()
    recorder = Recorder(fakes)
    document = upgrade(plan, recorder, execute=True)
    assert document["failed_step"] == "preflight" and recorder.runs == [] and fakes.calls == []


def test_a_checkout_at_another_revision_blocks(tmp_path, repo):
    repo["head"] = "c" * 40
    plan, _state = focused_plan(tmp_path, repo["root"])
    recorder = Recorder(Fakes())
    document = upgrade(plan, recorder, execute=True)
    assert document["failed_step"] == "preflight" and recorder.log == []
    assert document["focused_tests"]["result"] == "wrong-checkout"


def test_a_range_git_cannot_resolve_blocks(tmp_path, repo, monkeypatch):
    def broken(*_a, **_k):
        raise ValueError("git diff failed: unknown revision")

    monkeypatch.setattr(focused_tests, "changed_files", broken)
    plan, _state = focused_plan(tmp_path, repo["root"])
    recorder = Recorder(Fakes())
    document = upgrade(plan, recorder, execute=True)
    assert document["failed_step"] == "preflight" and recorder.log == []
    assert "could not be selected" in focused_check(document)["summary"]


def test_an_already_blocked_preflight_does_not_run_the_tests(tmp_path, repo):
    plan, _state = focused_plan(tmp_path, repo["root"])
    (tmp_path / "receipts").rmdir()  # the writable-directory proof fails first
    recorder = Recorder(Fakes())
    document = upgrade(plan, recorder, execute=True)
    assert document["failed_step"] == "preflight" and recorder.runs == []
    assert document["focused_tests"]["result"] == "not-run"
    assert focused_check(document)["status"] == "skipped"


def test_without_a_checkout_the_step_is_skipped_and_says_so(tmp_path, repo):
    empty = tmp_path / "not-a-checkout"
    empty.mkdir()
    plan, _state = focused_plan(tmp_path, empty)
    recorder = Recorder(Fakes())
    document = upgrade(plan, recorder, execute=True)
    assert document["result"] == "ok"  # skipped, not failed, and not a silent pass
    assert recorder.runs == []
    check = focused_check(document)
    assert check["status"] == "skipped"
    assert check["summary"].startswith("skipped: no repository checkout")
    assert document["focused_tests"]["result"] == "skipped"
    assert document["preflight"]["counts"]["skipped"] >= 1


def test_the_receipt_key_is_absent_when_the_plan_does_not_enable_it(tmp_path, monkeypatch):
    monkeypatch.setattr(focused_tests, "changed_files", pytest.fail)
    plan, _path, _state = make_plan(tmp_path)
    recorder = Recorder(Fakes())
    for execute in (False, True):
        document = upgrade(plan, recorder, execute=execute)
        assert "focused_tests" not in document
        assert all(c["id"] != "tests.focused" for c in document["preflight"]["checks"])
        assert not any("focused" in step for step in document.get("planned", []))
    assert recorder.runs == []


def test_the_written_receipt_carries_the_selection_and_no_path(tmp_path, repo):
    plan, _state = focused_plan(tmp_path, repo["root"])
    document = upgrade(plan, Recorder(Fakes()), execute=True)
    written = deploy_upgrade.write_receipt(plan, document)
    text = written.read_text()
    assert str(tmp_path) not in text
    assert json.loads(text)["focused_tests"]["files"] == ["tests/test_alpha.py"]


def test_the_default_runner_keeps_pytest_output_off_stdout(tmp_path, monkeypatch, capsys):
    started = []
    monkeypatch.setattr(
        deploy_upgrade.subprocess, "Popen", lambda argv, **options: started.append(options)
    )
    seen = {}

    def run(files, root, jobs=1, pytest_args=(), *, popen, **_):
        print("shard 0: exit 0")
        popen(["pytest"], cwd=root)
        seen.update(files=files, jobs=jobs, stderr=sys.stderr)
        return 0

    monkeypatch.setattr(focused_tests, "run", run)
    assert deploy_upgrade._default_focused_runner(["tests/test_x.py"], tmp_path, 2) == 0
    captured = capsys.readouterr()
    assert captured.out == "" and "shard 0" in captured.err
    assert started == [{"cwd": tmp_path, "stdout": seen["stderr"]}]
    assert seen["files"] == ["tests/test_x.py"] and seen["jobs"] == 2


def test_the_upgrade_asks_for_the_exact_difference_between_the_two_releases(tmp_path, repo):
    plan, _state = focused_plan(tmp_path, repo["root"])
    upgrade(plan, Recorder(Fakes()), execute=False)
    assert repo["ranges"] == [(f"{FROM}..{TO}", True)]


def test_one_unmapped_file_blocks_even_when_other_tests_are_selected(tmp_path, repo):
    # A mapped test change plus a file nothing maps to used to pass: it must block now.
    repo["changed"] = ["tests/test_alpha.py", "src/pkg/data.bin"]
    plan, _state = focused_plan(tmp_path, repo["root"])
    recorder = Recorder(Fakes())
    document = upgrade(plan, recorder, execute=True)
    assert document["result"] == "failed" and document["failed_step"] == "preflight"
    entry = document["focused_tests"]
    assert entry["result"] == "unmapped" and entry["unmapped"] == ["src/pkg/data.bin"]
    assert "tests/test_alpha.py" in entry["files"]
    assert recorder.log == []  # nothing ran, nothing stopped: the full suite is not a fallback


def test_a_file_declared_safe_does_not_block_and_is_recorded(tmp_path, repo):
    (repo["root"] / "dev").mkdir(exist_ok=True)
    (repo["root"] / "src" / "pkg" / "data").mkdir()
    (repo["root"] / "src" / "pkg" / "data" / "x.txt").write_text("x")
    (repo["root"] / focused_tests.OVERRIDES_FILE).write_text(
        '[unmapped]\nsafe = ["src/pkg/data/*.txt"]\n'
    )
    repo["changed"] = ["tests/test_alpha.py", "src/pkg/data/x.txt"]
    plan, _state = focused_plan(tmp_path, repo["root"])
    document = upgrade(plan, Recorder(Fakes()), execute=True)
    assert document["result"] == "ok", document["steps"]
    entry = document["focused_tests"]
    assert entry["unmapped"] == [] and entry["safe_unmapped"] == ["src/pkg/data/x.txt"]


def test_cuts_and_broad_selections_are_in_the_summary_and_the_receipt(tmp_path, repo):
    root = repo["root"]
    (root / "dev").mkdir(exist_ok=True)
    (root / "src" / "pkg" / "hub.py").write_text("X = 1\n")
    (root / "src" / "pkg" / "__init__.py").write_text("")
    for index in range(4):
        (root / "tests" / f"test_imp{index}.py").write_text("from pkg.hub import X\n")
    (root / focused_tests.OVERRIDES_FILE).write_text(
        '[[module]]\nmatch = ["src/pkg/hub.py"]\ncap = 2\n'
    )
    repo["changed"] = ["src/pkg/hub.py", "src/pkg/__init__.py"]
    plan, _state = focused_plan(tmp_path, root)
    document = upgrade(plan, Recorder(Fakes()), execute=False)
    entry = document["focused_tests"]
    assert entry["truncated"] and "dropped" in entry["truncated"][0]
    assert entry["broad"] == ["src/pkg/__init__.py"]
    check = focused_check(document)
    assert "cut by a cap" in check["summary"] and "broad" in check["summary"]
