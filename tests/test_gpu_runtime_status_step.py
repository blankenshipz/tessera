"""gpu_runtime_v1 failure status line: the closed ``step`` token naming the custody phase.

Offline: fake custody and the real ``main`` status line. ``step`` is one of the closed
phase tokens or absent; the guardian report is unchanged."""

import json
import time
from pathlib import Path

import pytest

pytest.importorskip("cryptography")
from test_gpu_runtime_failure_custody import OUTPUTS, Custody, exits, main_argv
from test_gpu_runtime_v1 import BLOBS, OP, opener_for, rt
from test_salad_input_path import InputCustody, argv_for, small_chunks, snapshot_of  # noqa: F401


class Refusing(Custody):
    def begin_once(self):
        self.log.append("begin")
        return False


class BadPlan(Custody):
    def output_plan(self):
        plan = super().output_plan()
        plan["chunk_bytes"] = rt.OUTPUT_CHUNK_BYTES // 2
        return plan


class BrokenUpload(Custody):
    def put_chunk(self, name, size, digest, seq, data):
        raise rt.Failure("custody")


class InputWithReport(InputCustody):
    def __init__(self, log, snap, **kw):
        super().__init__(log, snap, **kw)
        self.reports = []

    def report_failed(self, reason, exit_code):
        self.reports.append((reason, exit_code))
        return True


def drive(monkeypatch, capsys, tmp_path, argv, custody, entry=None):
    """Run the real ``main`` against ``custody``; return (exit code, status line)."""
    custody.log = getattr(custody, "log", [])

    def entry_runner(path, work, site, out_dir, dl, supervisor=None):
        if entry:
            entry(Path(out_dir), supervisor)
        else:
            (Path(out_dir) / "output.json").write_text('{"ok":1}')

    real = rt.execute

    def execute(request, deadline, work=None):
        return real(
            request, deadline, str(tmp_path / "work"),
            custody_factory=lambda b, d: custody, opener=opener_for(BLOBS),
            installer=lambda *a: "site", entry_runner=entry_runner,
        )  # fmt: skip

    monkeypatch.setattr(rt, "execute", execute)
    monkeypatch.setattr(rt, "LINGER_SECONDS", 0)
    code = rt.main(argv)
    return code, json.loads(capsys.readouterr().out)


def plain_argv():
    return main_argv(OUTPUTS)


def test_a_claim_refusal_names_the_claim_step_and_is_not_reported(monkeypatch, capsys, tmp_path):
    custody = Refusing([], OUTPUTS)
    code, line = drive(monkeypatch, capsys, tmp_path, plain_argv(), custody)
    assert code == 1
    assert line == {"schema": rt.STATUS_SCHEMA, "status": "failed", "reason": "custody",
                    "step": "claim"}  # fmt: skip
    assert custody.reports == []


def test_an_input_manifest_failure_names_the_manifest_step(monkeypatch, capsys, tmp_path):
    snap = snapshot_of()
    custody = InputWithReport([], snap, fail={"manifest": 99})
    code, line = drive(monkeypatch, capsys, tmp_path, argv_for(snap), custody)
    assert line == {"schema": rt.STATUS_SCHEMA, "status": "failed", "reason": "input_fetch",
                    "step": "manifest"}  # fmt: skip
    assert custody.reports == [("input", None)]


def test_an_input_chunk_failure_names_the_chunk_step(monkeypatch, capsys, tmp_path):
    snap = snapshot_of()
    custody = InputWithReport([], snap, fail={0: 99})
    code, line = drive(monkeypatch, capsys, tmp_path, argv_for(snap), custody)
    assert line == {"schema": rt.STATUS_SCHEMA, "status": "failed", "reason": "input_fetch",
                    "step": "chunk"}  # fmt: skip
    assert custody.reports == [("input", None)]


def test_an_output_plan_refusal_names_the_output_plan_step(monkeypatch, capsys, tmp_path):
    custody = BadPlan([], OUTPUTS)
    code, line = drive(monkeypatch, capsys, tmp_path, plain_argv(), custody)
    assert line == {"schema": rt.STATUS_SCHEMA, "status": "failed", "reason": "output_plan",
                    "step": "output_plan"}  # fmt: skip
    assert custody.reports == [("output_plan", None)]


def test_an_upload_failure_names_the_upload_step(monkeypatch, capsys, tmp_path):
    custody = BrokenUpload([], OUTPUTS)

    def entry(out, supervisor):
        (out / "output.json").write_text('{"answer":42}')
        (out / "weights.bin").write_bytes(b"w" * 100)
        (out / rt.CHECKPOINT_NAME).write_bytes(b"ckpt")

    code, line = drive(monkeypatch, capsys, tmp_path, plain_argv(), custody, entry)
    assert line == {"schema": rt.STATUS_SCHEMA, "status": "failed", "reason": "custody",
                    "step": "upload"}  # fmt: skip
    assert custody.reports == [("custody", None)]


def test_an_entry_failure_has_no_step_and_keeps_its_exit_code(monkeypatch, capsys, tmp_path):
    custody = Custody([], OUTPUTS)
    code, line = drive(monkeypatch, capsys, tmp_path, plain_argv(), custody, exits(3))
    assert "step" not in line
    assert line["reason"] == "entry" and line["exit_code"] == 3
    assert custody.reports == [("entry", 3)]


def test_only_closed_tokens_are_ever_printed(monkeypatch, capsys, tmp_path):
    def boom(*a, **k):
        error = rt.Failure("custody")
        error.step = "secret-detail"
        raise error

    monkeypatch.setattr(rt, "execute", boom)
    assert rt.main(plain_argv()) == 1
    assert "step" not in json.loads(capsys.readouterr().out)
    assert rt.STEPS == {"claim", "manifest", "chunk", "output_plan", "upload"}


def test_the_failure_report_body_is_unchanged_by_the_step():
    posts = []
    custody = rt.HTTPSCustody.__new__(rt.HTTPSCustody)
    custody.binding = {"operation": OP, "epoch": "7"}
    custody.token_expires = time.time() + 600
    custody.post = lambda path, body, **kw: posts.append((path, body)) or {
        "schema": "tessera.workload-failed.v1", "operation": OP, "epoch": "7", "recorded": True,
    }  # fmt: skip
    error = rt.Failure("custody")
    error.step = "upload"
    rt._report(custody, error)
    assert posts == [(
        "/v1/workload/failed",
        {"binding": {"operation": OP, "epoch": "7"}, "reason": "custody", "exit_code": None},
    )]  # fmt: skip
