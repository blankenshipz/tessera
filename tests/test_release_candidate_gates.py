"""Isolated gate-runner schema/evidence tests; no full integration commands run."""

import importlib.util
import io
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "rc_gates", Path(__file__).resolve().parents[1] / "scripts/run_release_candidate_gates.py"
)
gates = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gates)


@pytest.fixture
def candidate(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/example.py").write_text("answer = 385\n")
    return tmp_path, gates.manifest(tmp_path, ["src/example.py"])


def passing(root, name, timeout):
    return {
        "exit_code": 0,
        "category": "completed",
        "counts": {"passed": 3} if name == "python-tests" else {},
    }


def test_not_frozen_runs_no_command_and_retains_unrun_integration(candidate):
    root, value = candidate
    report = gates.run_gates(root, value, execute=lambda *_: pytest.fail("unexpected execution"))
    assert report["local_gate_status"] == "incomplete"
    assert all(r["status"] == "not_run" for r in report["source_gates"])
    assert all(
        r["mode"] == "live" and r["status"] == "not_run" for r in report["required_integration"]
    )
    assert report["full_acceptance"] == "not_run"


def test_synthetic_executor_never_becomes_native_pass(candidate):
    root, value = candidate
    report = gates.run_gates(root, value, parent_frozen=True, execute=passing)
    assert report["local_gate_status"] == "synthetic_only"
    assert all(r["mode"] == "synthetic" for r in report["source_gates"])
    assert report["source_sha256_before"] == report["source_sha256_after"]


@pytest.mark.parametrize(
    "result",
    [
        {"exit_code": None, "category": "command_unavailable", "counts": {}},
        {"exit_code": 1, "category": "completed", "counts": {"failed": 1}},
        {"exit_code": -9, "category": "timeout", "counts": {}},
        {"exit_code": 0, "category": "output_not_closed", "counts": {}},
    ],
)
def test_missing_failed_timed_out_or_incomplete_commands_never_pass(candidate, result):
    root, value = candidate
    report = gates.run_gates(root, value, parent_frozen=True, execute=lambda *_: result)
    assert all(r["status"] == "failed" for r in report["source_gates"])
    assert report["local_gate_status"] == "incomplete"


@pytest.mark.parametrize(
    "counts",
    [
        {},
        {"passed": 3, "skipped": 1},
        {"passed": 3, "deselected": 1},
        {"passed": 3, "xfailed": 1},
        {"error": 1},
    ],
)
def test_zero_tests_and_skipped_coverage_never_satisfy_full_pytest(candidate, counts):
    root, value = candidate
    report = gates.run_gates(
        root,
        value,
        parent_frozen=True,
        execute=lambda *_: {"exit_code": 0, "category": "completed", "counts": counts},
    )
    assert report["source_gates"][-1]["status"] == "incomplete"


def test_source_change_invalidates_passing_receipts(candidate):
    root, value = candidate

    def change(*args):
        (root / "src/example.py").write_text("changed = True\n")
        return passing(*args)

    report = gates.run_gates(root, value, parent_frozen=True, execute=change)
    assert report["source_unchanged"] is False
    assert all(r["status"] == "invalidated" for r in report["source_gates"])


def test_expected_revision_mismatch_never_executes(candidate):
    root, value = candidate
    value["source_sha256"] = "0" * 64
    report = gates.run_gates(
        root, value, parent_frozen=True, execute=lambda *_: pytest.fail("unexpected execution")
    )
    assert all(r["status"] == "not_run" for r in report["source_gates"])


def test_added_source_invalidates_exact_scope(candidate):
    root, value = candidate

    def change(*args):
        (root / "src/added.py").write_text("new = True\n")
        return passing(*args)

    report = gates.run_gates(root, value, parent_frozen=True, execute=change)
    assert report["source_sha256_after"] is None
    assert report["local_gate_status"] == "incomplete"


def test_omitted_source_never_admits_gate_execution(candidate):
    root, value = candidate
    (root / "src/omitted.py").write_text("new = True\n")
    with pytest.raises(gates.GateInputError, match="source_scope_incomplete"):
        gates.run_gates(root, value, parent_frozen=True, execute=passing)


@pytest.mark.parametrize(
    "path",
    [
        "../README.md",
        ".env",
        "src/.env.production",
        "state/auth.header",
        "src/runtime/token",
        "src/private.pem",
        "/tmp/source.py",
    ],
)
def test_source_scope_denies_runtime_secret_and_escaping_paths(path):
    with pytest.raises(gates.GateInputError):
        gates.source_path(path)


def test_symlink_never_reads_external_source(candidate, tmp_path):
    root, value = candidate
    (root / "src/example.py").unlink()
    (root / "src/example.py").symlink_to("/unavailable/private/key")
    with pytest.raises(gates.GateInputError, match="source_missing_or_symlink"):
        gates.source_hash(root, value["source_paths"])


def test_unapproved_commands_and_freeform_artifact_metadata_denied(candidate):
    _, value = candidate
    with pytest.raises(gates.GateInputError):
        gates.validate_manifest(value | {"gates": ["host-command"]})
    with pytest.raises(gates.GateInputError):
        gates.validate_manifest(
            value
            | {
                "artifacts": [
                    {"subject": "wheel", "sha256": "a" * 64, "token": "never report this"}
                ]
            }
        )


def test_transient_output_parser_records_only_summary_counts():
    counts = {}
    gates._consume(
        io.BytesIO(
            b"raw private prompt/header should be discarded\n"
            b"pytest traceback source contents discarded\n"
            b"3 passed, 2 warnings in 0.20s\n"
        ),
        counts,
    )
    assert counts == {"passed": 3, "warning": 2}


def test_native_missing_process_reports_finite_category(candidate, monkeypatch):
    root, _ = candidate

    def unavailable(*args, **kwargs):
        raise FileNotFoundError("private resource must not enter report")

    monkeypatch.setattr(gates.subprocess, "Popen", unavailable)
    assert gates.native_gate(root, "ruff-lint", 1) == {
        "exit_code": None,
        "category": "command_unavailable",
        "counts": {},
    }


def test_named_standard_gate_argv_only_and_no_raw_output_retained(candidate, monkeypatch):
    root, _ = candidate
    seen = []

    class FakeProcess:
        returncode = 0
        stdout = io.BytesIO(b"private error body discarded\n3 passed in 0.01s\n")

        def wait(self, timeout):
            assert timeout == 1
            return 0

    def start(argv, **kwargs):
        seen.append(argv)
        assert kwargs["cwd"] == root and kwargs["start_new_session"] is True
        return FakeProcess()

    monkeypatch.setattr(gates.subprocess, "Popen", start)
    assert gates.native_gate(root, "python-tests", 1) == {
        "exit_code": 0,
        "category": "completed",
        "counts": {"passed": 3},
    }
    assert seen == [[gates.sys.executable, "-m", "pytest", "-q"]]


@pytest.mark.parametrize(
    "path",
    [
        "src/auth.json",
        "dev/credentials.json",
        "plugins/token.json",
        "docs/tokens.json",
        "docs/owner.header",
        "docs/worker.header",
        "deploy/credentials",
        "deploy/token",
        "docs/client.pem",
        "docs/client.key",
        "docs/control.sqlite3",
        "docs/control.db",
    ],
)
def test_auth_data_is_not_source(path, tmp_path, monkeypatch):
    fixture = tmp_path / path
    fixture.parent.mkdir(parents=True, exist_ok=True)
    fixture.write_bytes(b"SYNTHETIC_EXCLUDED_NOT_A_SECRET\n")
    monkeypatch.setattr(Path, "read_bytes", lambda _: pytest.fail("Excluded file was read"))
    with pytest.raises(gates.GateInputError, match="excluded_source_path"):
        gates.source_path(path)
    assert path not in gates.source_inventory(tmp_path)


def test_arbitrary_status_or_error_body_cannot_enter_report(candidate):
    root, value = candidate
    with pytest.raises(gates.GateInputError, match="invalid_executor_receipt"):
        gates.run_gates(
            root,
            value,
            parent_frozen=True,
            execute=lambda *_: {"exit_code": 0, "category": "private error contents", "counts": {}},
        )


@pytest.fixture
def observer_candidate(candidate):
    root, _ = candidate
    bundle = root / "release/opencode-observer"
    bundle.mkdir(parents=True)
    for name in gates.OBSERVER_FILES:
        (bundle / name).write_text("public distribution fixture\n")
    paths = gates.source_inventory(root)
    assert set(paths) == {"src/example.py"} | {
        "release/opencode-observer/" + name for name in gates.OBSERVER_FILES
    }
    return root, gates.manifest(root, paths)


def test_observer_patch_drift_invalidates_revision_and_receipts(observer_candidate):
    root, value = observer_candidate

    def change(*args):
        (root / "release/opencode-observer/observer.patch").write_text("changed patch\n")
        return passing(*args)

    report = gates.run_gates(root, value, parent_frozen=True, execute=change)
    assert report["source_sha256_before"] != report["source_sha256_after"]
    assert report["source_unchanged"] is False
    assert all(row["status"] == "invalidated" for row in report["source_gates"])


def test_omitted_observer_bundle_blocks_execution(observer_candidate):
    root, _ = observer_candidate
    omitted = gates.manifest(root, ["src/example.py"])
    with pytest.raises(gates.GateInputError, match="source_scope_incomplete"):
        gates.run_gates(
            root,
            omitted,
            parent_frozen=True,
            execute=lambda *_: pytest.fail("omitted bundle must not execute gates"),
        )


@pytest.mark.parametrize(
    "path",
    [
        "release/runtime/config.json",
        "release/opencode-observer/private-guard.mjs",
        "release/opencode-observer/vendor/source.ts",
        "release/opencode-observer/credentials.json",
        "release/other-observer/observer.patch",
        "release/opencode-observer/LICENSE.extra",
    ],
)
def test_release_scope_is_exact_closed_distribution_only(path):
    with pytest.raises(gates.GateInputError):
        gates.source_path(path)


def test_baseline_archive_drift_invalidates_receipts(observer_candidate):
    root, value = observer_candidate

    def change(*args):
        (root / "release/opencode-observer/baseline.tar.gz").write_bytes(b"changed archive bytes")
        return passing(*args)

    report = gates.run_gates(root, value, parent_frozen=True, execute=change)
    assert report["source_sha256_before"] != report["source_sha256_after"]
    assert all(row["status"] == "invalidated" for row in report["source_gates"])


def test_ignored_baseline_archive_omission_blocks_execution(observer_candidate, monkeypatch):
    from types import SimpleNamespace

    root, value = observer_candidate
    archive = "release/opencode-observer/baseline.tar.gz"
    (root / ".git").mkdir()
    git_paths = [p for p in value["source_paths"] if p != archive]
    monkeypatch.setattr(
        gates.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(stdout=("\0".join(git_paths) + "\0").encode()),
    )
    assert gates.source_inventory(root) == value["source_paths"]
    omitted = gates.manifest(root, git_paths)
    with pytest.raises(gates.GateInputError, match="source_scope_incomplete"):
        gates.run_gates(
            root,
            omitted,
            parent_frozen=True,
            execute=lambda *_: pytest.fail("ignored archive omission must not execute"),
        )


@pytest.mark.parametrize(
    "path", ["release/opencode-observer/extra.tar.gz", "release/runtime/baseline.tar.gz"]
)
def test_unapproved_release_archives_are_not_admitted(path):
    with pytest.raises(gates.GateInputError):
        gates.source_path(path)
