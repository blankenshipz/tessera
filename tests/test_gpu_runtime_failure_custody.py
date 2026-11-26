"""gpu_runtime_v1 failure custody: closed failure report, partial outputs, checkpoint,
diagnostic tail and the stop wait. Offline: fake custody, no provider, no network."""

import json
import signal
import time
from pathlib import Path

import pytest
from test_gpu_runtime_v1 import BLOBS, OP, WATCHDOG, FakeCustody, deadline, mf, opener_for, rt, sha
from test_gpu_runtime_v1 import manifest_for as manifest_for_blobs

from tessera_control import salad_workload_contract as contract

OUTPUTS = [
    {"name": "output.json", "max_bytes": 4096},
    {"name": "weights.bin", "max_bytes": 4096},
    {"name": rt.CHECKPOINT_NAME, "max_bytes": 4096},
    {"name": rt.DIAGNOSTIC_NAME, "max_bytes": 2048},
]


class Custody(FakeCustody):
    """FakeCustody plus the failure report and the superseding checkpoint."""

    def __init__(self, log, outputs=None):
        super().__init__(log, outputs=outputs or OUTPUTS)
        self.reports = []
        self.identity = {}
        self.report_ok = True

    def put_chunk(self, name, size, digest, seq, data):
        if name == rt.CHECKPOINT_NAME and self.identity.get(name, digest) != digest:
            self.parts.pop(name, None)
            self.done.discard(name)
        self.identity[name] = digest
        return super().put_chunk(name, size, digest, seq, data)

    def report_failed(self, reason, exit_code):
        self.done_at_report = set(self.done)
        self.log.append(("failed", reason, exit_code))
        self.reports.append((reason, exit_code))
        return self.report_ok


def request(outputs):
    raw = mf.canonical(manifest_for_blobs(BLOBS))
    total = str(sum(o["max_bytes"] for o in outputs))
    argv = ["v1", OP, "7", "custody.example.com", WATCHDOG, "120", total, str(len(outputs)),
            sha(raw), *mf.refs(raw)]  # fmt: skip
    return rt.parse_argv(argv)


def run(tmp_path, entry, outputs=OUTPUTS, custody=None):
    log = []
    custody = custody or Custody(log, outputs)
    custody.log = log

    def entry_runner(path, work, site, out_dir, dl, supervisor=None):
        log.append("entry")
        entry(Path(out_dir), supervisor)

    def go():
        return rt.execute(
            request(outputs), deadline(), str(tmp_path / "work"),
            custody_factory=lambda b, d: custody, opener=opener_for(BLOBS),
            installer=lambda *a: "site", entry_runner=entry_runner,
        )  # fmt: skip

    return go, custody, log


def exits(code):
    def entry(out, supervisor):
        error = rt.Failure("entry")
        error.exit_code = code
        raise error

    return entry


def full(out, supervisor):
    (out / "output.json").write_text('{"answer":42}')
    (out / "weights.bin").write_bytes(b"w" * 100)
    (out / rt.CHECKPOINT_NAME).write_bytes(b"ckpt")


# ---------------------------------------------------------------- failure report


def test_an_entry_that_exits_3_reports_exactly_one_failure_with_its_exit_code(tmp_path):
    go, custody, log = run(tmp_path, exits(3))
    with pytest.raises(rt.Failure) as caught:
        go()
    assert custody.reports == [("entry", 3)]
    assert caught.value.reported is True and caught.value.exit_code == 3
    # the report precedes only the last file (the diagnostic), which would complete the outputs
    chunks = [i for i, e in enumerate(log) if e[0] == "chunk"]
    assert chunks[-1] > log.index(("failed", "entry", 3)) > chunks[-2]
    assert log[chunks[-1]][1] == rt.DIAGNOSTIC_NAME


def test_a_failure_before_the_claim_is_not_reported(tmp_path):
    go, custody, log = run(tmp_path, full)
    custody.begin_once = lambda: False
    with pytest.raises(rt.Failure):
        go()
    assert custody.reports == []


def test_a_failure_after_the_claim_outside_the_entry_is_reported_with_a_closed_reason(tmp_path):
    go, custody, _ = run(tmp_path, full, outputs=[{"name": "output.json", "max_bytes": 4096}])
    custody.output_plan = lambda: {"outputs": [], "chunk_bytes": 1}
    with pytest.raises(rt.Failure) as caught:
        go()
    assert caught.value.code == "output_plan" and custody.reports == [("output_plan", None)]


def test_an_unconfirmed_report_is_retried_once_then_not_claimed(tmp_path):
    go, custody, _ = run(tmp_path, exits(1))
    custody.report_ok = False
    with pytest.raises(rt.Failure) as caught:
        go()
    assert len(custody.reports) == 2 and caught.value.reported is False


@pytest.mark.parametrize(
    "code,reason",
    [("entry", "entry"), ("input_chunk", "input"), ("manifest_digest", "manifest"),
     ("redirect", "fetch"), ("argv", "manifest"), ("surprise", "internal")],
)  # fmt: skip
def test_failure_codes_map_onto_the_closed_reasons(code, reason):
    assert rt.failure_reason(code) == reason
    assert reason in contract.WORKLOAD_FAILURE_REASONS


def test_runtime_and_contract_agree_on_names_and_reasons():
    assert rt.FAILURE_REASONS == contract.WORKLOAD_FAILURE_REASONS
    assert rt.CHECKPOINT_NAME == contract.CHECKPOINT_OUTPUT_NAME
    assert rt.DIAGNOSTIC_NAME == contract.DIAGNOSTIC_OUTPUT_NAME
    assert rt.MAX_CHECKPOINT_UPLOADS == contract.MAX_CHECKPOINT_UPLOADS
    assert rt.FAILED_PATH == "/v1/workload/failed"


def test_custody_client_posts_only_binding_reason_and_exit_code(monkeypatch):
    posts = []
    custody = rt.HTTPSCustody.__new__(rt.HTTPSCustody)
    custody.binding = {"operation": OP, "epoch": "7"}
    custody.token_expires = time.time() + 600
    custody.post = lambda path, body, **kw: posts.append((path, body)) or {
        "schema": "tessera.workload-failed.v1", "operation": OP, "epoch": "7", "recorded": True,
    }  # fmt: skip
    assert custody.report_failed("entry", 3) is True
    assert posts == [(
        "/v1/workload/failed",
        {"binding": {"operation": OP, "epoch": "7"}, "reason": "entry", "exit_code": 3},
    )]  # fmt: skip


# ---------------------------------------------------------------- partial outputs


def test_a_cut_off_entry_uploads_what_exists_marked_partial(tmp_path):
    script = tmp_path / "trainer.py"
    script.write_text(
        "import os, sys, time\n"
        "out = sys.argv[2]\n"
        "print('soft', os.environ['TESSERA_ENTRY_SOFT_DEADLINE_SECONDS'], flush=True)\n"
        "open(os.path.join(out, 'tmp'), 'wb').write(b'step-19999')\n"
        "os.replace(os.path.join(out, 'tmp'), os.path.join(out, 'checkpoint.bin'))\n"
        "open(os.path.join(out, 'output.json'), 'w').write('{\"step\": 19999}')\n"
        "print('training step 19999', flush=True)\n"
        "time.sleep(60)\n"
    )

    def entry(out, supervisor):
        rt.run_entry(
            str(script), str(tmp_path), "site", str(out), deadline(3), supervisor=supervisor
        )

    go, custody, _ = run(tmp_path, entry)
    started = time.monotonic()
    with pytest.raises(rt.Failure) as caught:
        go()
    assert time.monotonic() - started < 20
    failure = caught.value
    assert failure.code == "entry_timeout" and failure.partial and failure.reported
    assert custody.reports == [("entry_timeout", -signal.SIGTERM)]
    assert custody.file(rt.CHECKPOINT_NAME) == b"step-19999"
    marked = json.loads(custody.file("output.json"))
    assert marked["step"] == 19999 and marked["partial"] is True
    assert marked["partial_reason"] == "entry_timeout" and marked["partial_skipped"] == [
        "weights.bin"
    ]
    assert "weights.bin" not in custody.parts  # missing: reported, not fatal
    assert sorted(o["name"] for o in failure.outputs) == sorted(
        ["output.json", rt.CHECKPOINT_NAME, rt.DIAGNOSTIC_NAME]
    )
    diagnostic = custody.file(rt.DIAGNOSTIC_NAME).decode()
    assert "training step 19999" in diagnostic and "soft 1" in diagnostic
    assert json.loads(diagnostic.splitlines()[0])["status"] == "partial"


def test_the_entry_can_write_a_final_checkpoint_when_it_gets_sigterm(tmp_path):
    script = tmp_path / "trainer.py"
    script.write_text(
        "import os, signal, sys, time\n"
        "out = sys.argv[2]\n"
        "def final(*_):\n"
        "    with open(os.path.join(out, 'checkpoint.tmp'), 'wb') as f:\n"
        "        f.write(b'final'); f.flush(); os.fsync(f.fileno())\n"
        "    os.replace(os.path.join(out, 'checkpoint.tmp'), os.path.join(out, 'checkpoint.bin'))\n"
        "    raise SystemExit(0)\n"
        "signal.signal(signal.SIGTERM, final)\n"
        "time.sleep(60)\n"
    )

    def entry(out, supervisor):
        rt.run_entry(
            str(script), str(tmp_path), "site", str(out), deadline(3), supervisor=supervisor
        )

    go, custody, _ = run(tmp_path, entry)
    with pytest.raises(rt.Failure) as caught:
        go()
    # a clean exit after our SIGTERM is still a cut-off run, never a success
    assert caught.value.code == "entry_timeout" and caught.value.exit_code == 0
    assert custody.file(rt.CHECKPOINT_NAME) == b"final"


def test_a_hard_deadline_kills_an_entry_that_ignores_sigterm(tmp_path):
    script = tmp_path / "stubborn.py"
    script.write_text(
        "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(60)\n"
    )
    supervisor = rt.EntrySupervisor()
    started = time.monotonic()
    with pytest.raises(rt.Failure) as caught:
        rt.run_entry(str(script), str(tmp_path), "site", str(tmp_path), deadline(2),
                     supervisor=supervisor)  # fmt: skip
    assert time.monotonic() - started < 15
    assert caught.value.code == "entry_timeout" and caught.value.exit_code == -signal.SIGKILL


def test_partial_mode_keeps_every_per_file_cap(tmp_path):
    def entry(out, supervisor):
        (out / "weights.bin").write_bytes(b"x" * 5000)  # over its 4096 cap
        (out / rt.CHECKPOINT_NAME).write_bytes(b"ok")
        raise rt.Failure("entry")

    go, custody, _ = run(tmp_path, entry)
    with pytest.raises(rt.Failure) as caught:
        go()
    assert "weights.bin" not in custody.parts and caught.value.partial
    assert json.loads(custody.file("output.json"))["partial_skipped"] == ["weights.bin"]


def test_partial_mode_never_follows_a_symlink_or_reads_a_directory(tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("private")

    def entry(out, supervisor):
        (out / "weights.bin").symlink_to(secret)
        (out / rt.CHECKPOINT_NAME).mkdir()
        raise rt.Failure("entry")

    go, custody, _ = run(tmp_path, entry)
    with pytest.raises(rt.Failure):
        go()
    assert set(custody.parts) == {"output.json", rt.DIAGNOSTIC_NAME}


def test_a_successful_entry_still_needs_every_declared_output(tmp_path):
    def entry(out, supervisor):
        (out / "output.json").write_text("{}")

    go, custody, _ = run(tmp_path, entry)
    with pytest.raises(rt.Failure) as caught:
        go()
    assert caught.value.code == "output" and not caught.value.partial
    assert not [e for e in custody.log if isinstance(e, tuple) and e[0] == "chunk"]
    assert custody.reports == [("output", None)]


# ----------------------------------------------------------------- checkpoint


def test_a_temp_only_checkpoint_is_never_uploaded_and_a_later_one_supersedes(tmp_path):
    seen = {}

    def entry(out, supervisor):
        clock = [0.0]
        supervisor.clock = lambda: clock[0]
        supervisor.tick()  # starts the interval
        (out / ".checkpoint.bin.tmp").write_bytes(b"half-writt")
        (out / "checkpoint.bin.tmp").write_bytes(b"half-writt")
        clock[0] += 500
        supervisor.tick()
        seen["after_temp"] = set(custody.parts)
        (out / ".t").write_bytes(b"v1")
        (out / ".t").replace(out / rt.CHECKPOINT_NAME)
        clock[0] += 500
        supervisor.tick()
        seen["v1"] = custody.file(rt.CHECKPOINT_NAME)
        (out / ".t").write_bytes(b"v2-longer")
        (out / ".t").replace(out / rt.CHECKPOINT_NAME)
        clock[0] += 500
        supervisor.tick()
        seen["v2"] = custody.file(rt.CHECKPOINT_NAME)
        clock[0] += 500
        supervisor.tick()  # unchanged: nothing to supersede
        seen["uploads"] = len([e for e in custody.log if e[:2] == ("chunk", rt.CHECKPOINT_NAME)])
        full(out, supervisor)
        (out / rt.CHECKPOINT_NAME).write_bytes(b"v3")

    go, custody, _ = run(tmp_path, entry)
    outputs = go()
    assert seen["after_temp"] == set()
    assert seen["v1"] == b"v1" and seen["v2"] == b"v2-longer" and seen["uploads"] == 2
    assert custody.file(rt.CHECKPOINT_NAME) == b"v3"  # the final upload supersedes too
    assert {o["name"] for o in outputs} == {o["name"] for o in OUTPUTS}
    assert not (tmp_path / "work" / "ckpt").exists() or not list(
        (tmp_path / "work" / "ckpt").iterdir()
    )


def test_the_snapshot_reads_only_the_declared_name_never_a_symlink_or_oversize_file(tmp_path):
    out, stage = tmp_path / "out", tmp_path / "stage"
    out.mkdir()
    (out / "other").write_bytes(b"x")
    assert rt.snapshot_checkpoint(str(out), str(stage), 100, 1) is None
    (out / rt.CHECKPOINT_NAME).symlink_to(out / "other")
    assert rt.snapshot_checkpoint(str(out), str(stage), 100, 2) is None
    (out / rt.CHECKPOINT_NAME).unlink()
    (out / rt.CHECKPOINT_NAME).write_bytes(b"y" * 101)
    assert rt.snapshot_checkpoint(str(out), str(stage), 100, 3) is None
    assert list(stage.iterdir()) == []
    (out / rt.CHECKPOINT_NAME).write_bytes(b"y" * 100)
    name, path, size, digest = rt.snapshot_checkpoint(str(out), str(stage), 100, 4)
    assert (name, size, digest) == (rt.CHECKPOINT_NAME, 100, sha(b"y" * 100))


def test_a_failed_periodic_upload_never_fails_the_entry():
    supervisor = rt.EntrySupervisor(upload=lambda: (_ for _ in ()).throw(rt.Failure("custody")))
    clock = [0.0]
    supervisor.clock = lambda: clock[0]
    supervisor.tick()
    clock[0] += 1000
    supervisor.tick()


def test_periodic_checkpoint_needs_the_diagnostic_so_it_cannot_complete_the_outputs(tmp_path):
    only = [{"name": rt.CHECKPOINT_NAME, "max_bytes": 64}]
    captured = {}

    def entry(out, supervisor):
        captured["upload"] = supervisor.upload
        (out / rt.CHECKPOINT_NAME).write_bytes(b"c")

    go, _, _ = run(tmp_path, entry, outputs=only)
    go()
    assert captured["upload"] is None


# ------------------------------------------------------------------ diagnostics


@pytest.mark.parametrize("mode", ["success", "failure", "timeout"])
def test_the_diagnostic_tail_is_present_and_capped_on_every_path(tmp_path, mode):
    def entry(out, supervisor):
        supervisor.feed(b"noise " * 40000 + b"LAST-LINE")
        if mode == "success":
            return full(out, supervisor)
        full(out, supervisor)
        error = rt.Failure("entry" if mode == "failure" else "entry_timeout")
        error.exit_code = 7 if mode == "failure" else -9
        raise error

    go, custody, _ = run(tmp_path, entry)
    if mode == "success":
        go()
    else:
        with pytest.raises(rt.Failure):
            go()
    data = custody.file(rt.DIAGNOSTIC_NAME)
    header = json.loads(data.split(b"\n", 1)[0])
    assert len(data) <= 2048 and data.endswith(b"LAST-LINE")
    assert header["schema"] == rt.DIAGNOSTIC_SCHEMA and header["truncated"] is True
    assert header["status"] == ("complete" if mode == "success" else "partial")


def test_the_supervisor_keeps_only_the_last_64_kib_of_output(tmp_path):
    script = tmp_path / "chatty.py"
    script.write_text(
        "import sys\nsys.stdout.write('o' * 300000)\nsys.stderr.write('e' * 100)\nprint('END')\n"
    )
    supervisor = rt.EntrySupervisor()
    rt.run_entry(str(script), str(tmp_path), "site", str(tmp_path), deadline(30),
                 supervisor=supervisor)  # fmt: skip
    tail, truncated = supervisor.tail()
    assert len(tail) == rt.DIAGNOSTIC_TAIL_BYTES and truncated and tail.endswith(b"END\n")


def test_a_traceback_reaches_the_diagnostic_and_the_exit_code_is_kept(tmp_path):
    script = tmp_path / "boom.py"
    script.write_text("raise RuntimeError('kaboom')\n")
    supervisor = rt.EntrySupervisor()
    with pytest.raises(rt.Failure) as caught:
        rt.run_entry(str(script), str(tmp_path), "site", str(tmp_path), deadline(30),
                     supervisor=supervisor)  # fmt: skip
    assert caught.value.code == "entry" and caught.value.exit_code == 1
    assert b"RuntimeError: kaboom" in supervisor.tail()[0]


def test_the_diagnostic_never_exceeds_a_tiny_declared_cap():
    data = rt.diagnostic_bytes(b"t" * 500, False, "partial", 3, 60)
    assert len(data) <= 60


# ------------------------------------------------------------------- stop wait


class Clock:
    def __init__(self):
        self.now, self.slept = 0.0, []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def test_linger_survives_five_seconds_and_ends_only_at_the_bound():
    clock = Clock()
    stop = rt.threading.Event()
    assert rt.linger(120, clock=clock, sleep=clock.sleep, stopped=stop) is False
    assert clock.now == 120 and sum(clock.slept) == 120


def test_linger_ends_on_sigterm_when_the_guardian_stop_lands_forty_seconds_later():
    clock, stop = Clock(), rt.threading.Event()

    def sleep(seconds):
        clock.sleep(seconds)
        if clock.now >= 40:
            stop.set()

    assert rt.linger(120, clock=clock, sleep=sleep, stopped=stop) is True
    assert 40 <= clock.now < 42


def main_argv(outputs):
    raw = mf.canonical(manifest_for_blobs(BLOBS))
    total = str(sum(o["max_bytes"] for o in outputs))
    return ["v1", OP, "7", "custody.example.com", WATCHDOG, "120", total, str(len(outputs)),
            sha(raw), *mf.refs(raw)]  # fmt: skip


@pytest.fixture
def fake_clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(rt, "MONOTONIC", clock)
    monkeypatch.setattr(rt, "SLEEP", clock.sleep)
    rt._STOP.clear()
    previous = signal.getsignal(signal.SIGTERM)
    yield clock
    signal.signal(signal.SIGTERM, previous)
    rt._STOP.clear()


def test_after_the_last_chunk_the_runtime_is_still_alive_five_seconds_later(
    monkeypatch, fake_clock, capsys
):
    monkeypatch.setattr(rt, "execute", lambda *a, **k: [{"name": "output.json"}])
    alive = []
    original = fake_clock.sleep

    def sleep(seconds):
        original(seconds)
        alive.append(fake_clock.now)

    monkeypatch.setattr(rt, "SLEEP", sleep)
    assert rt.main(main_argv(OUTPUTS)) == 0
    assert any(t >= 5 for t in alive) and alive[-1] == rt.LINGER_SECONDS
    assert '"status":"retained"' in capsys.readouterr().out


def test_main_stops_waiting_on_sigterm(monkeypatch, fake_clock):
    monkeypatch.setattr(rt, "execute", lambda *a, **k: [])
    original = fake_clock.sleep

    def sleep(seconds):
        original(seconds)
        if fake_clock.now >= 40:
            signal.raise_signal(signal.SIGTERM)  # the installed handler sets the stop flag

    monkeypatch.setattr(rt, "SLEEP", sleep)
    assert rt.main(main_argv(OUTPUTS)) == 0
    assert 40 <= fake_clock.now < 42


def test_a_reported_failure_prints_the_exit_code_and_waits_for_the_stop(
    monkeypatch, fake_clock, capsys
):
    def boom(*a, **k):
        error = rt.Failure("entry")
        error.exit_code, error.reported = 3, True
        error.partial, error.outputs = True, [{"name": "output.json"}]
        raise error

    monkeypatch.setattr(rt, "execute", boom)
    assert rt.main(main_argv(OUTPUTS)) == 0 and fake_clock.now == rt.LINGER_SECONDS
    line = json.loads(capsys.readouterr().out)
    assert line["status"] == "failed" and line["reason"] == "entry" and line["exit_code"] == 3
    assert line["partial"] is True


def test_an_unreported_failure_still_exits_at_once(monkeypatch, fake_clock, capsys):
    monkeypatch.setattr(rt, "execute", lambda *a, **k: (_ for _ in ()).throw(rt.Failure("custody")))
    assert rt.main(main_argv(OUTPUTS)) == 1 and fake_clock.now == 0
    assert json.loads(capsys.readouterr().out)["reason"] == "custody"


def test_the_entry_environment_names_both_deadlines(tmp_path):
    script = tmp_path / "env.py"
    script.write_text("import os\nprint(os.environ['TESSERA_ENTRY_SOFT_DEADLINE_SECONDS'],"
                      " os.environ['TESSERA_ENTRY_HARD_DEADLINE_SECONDS'])\n")  # fmt: skip
    supervisor = rt.EntrySupervisor()
    rt.run_entry(str(script), str(tmp_path), "site", str(tmp_path), deadline(100),
                 supervisor=supervisor)  # fmt: skip
    soft, hard = supervisor.tail()[0].split()
    assert abs(int(hard) - int(soft) - rt.ENTRY_TERM_GRACE_SECONDS) <= 1


# ----------------------------------------- review findings: completion order and reporting


def every_output_then_exit_3(out, supervisor):
    full(out, supervisor)
    error = rt.Failure("entry")
    error.exit_code = 3
    raise error


def test_a_failed_entry_that_wrote_every_output_is_reported_before_the_outputs_complete(tmp_path):
    go, custody, log = run(tmp_path, every_output_then_exit_3)
    with pytest.raises(rt.Failure) as caught:
        go()
    names = {o["name"] for o in OUTPUTS}
    assert custody.reports == [("entry", 3)] and caught.value.reported
    assert custody.done_at_report == names - {rt.DIAGNOSTIC_NAME}  # not yet complete
    assert custody.done == names  # the diagnostic still lands after the report
    assert json.loads(custody.file("output.json"))["partial"] is True


def test_a_custody_error_during_the_partial_upload_keeps_the_entrys_reason(tmp_path):
    go, custody, _ = run(tmp_path, every_output_then_exit_3)
    custody.fail = {("weights.bin", 0): 99}
    with pytest.raises(rt.Failure) as caught:
        go()
    assert caught.value.code == "entry" and custody.reports == [("entry", 3)]


class ClosingCustody(Custody):
    """Writers close once every declared output is complete, as the guardian does."""

    closed = False

    def put_chunk(self, name, size, digest, seq, data):
        if self.closed:
            raise rt.Failure("custody")
        result = super().put_chunk(name, size, digest, seq, data)
        self.closed = result[1]
        return result


def test_the_diagnostic_is_uploaded_last_whatever_order_the_contract_declares(tmp_path):
    outputs = [
        {"name": "output.json", "max_bytes": 4096},
        {"name": rt.DIAGNOSTIC_NAME, "max_bytes": 2048},
        {"name": rt.CHECKPOINT_NAME, "max_bytes": 4096},
    ]
    custody = ClosingCustody([], outputs)

    def entry(out, supervisor):
        clock = [0.0]
        supervisor.clock = lambda: clock[0]
        supervisor.tick()
        (out / rt.CHECKPOINT_NAME).write_bytes(b"periodic")
        clock[0] += 500
        supervisor.tick()  # the checkpoint is complete in custody early
        (out / rt.CHECKPOINT_NAME).write_bytes(b"final-and-newer")
        (out / "output.json").write_text("{}")

    go, custody, log = run(tmp_path, entry, outputs=outputs, custody=custody)
    go()
    assert custody.file(rt.CHECKPOINT_NAME) == b"final-and-newer"
    chunks = [e[1] for e in log if isinstance(e, tuple) and e[0] == "chunk"]
    assert chunks[-1] == rt.DIAGNOSTIC_NAME


def test_stage_outputs_orders_the_diagnostic_last(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    for name in ("output.json", rt.CHECKPOINT_NAME):
        (out / name).write_text("{}" if name == "output.json" else "c")
    plan = [("output.json", 99), (rt.DIAGNOSTIC_NAME, 2048), (rt.CHECKPOINT_NAME, 99)]
    staged = rt.stage_outputs(str(out), str(tmp_path / "s"), plan, [],
                              diagnostic=(b"t", False, "complete", 0))  # fmt: skip
    assert [s[0] for s in staged] == ["output.json", rt.CHECKPOINT_NAME, rt.DIAGNOSTIC_NAME]


@pytest.mark.parametrize("how", ["refused", "raises"])
def test_a_lost_report_never_uploads_the_completing_file(tmp_path, how):
    go, custody, log = run(tmp_path, every_output_then_exit_3)
    if how == "refused":
        custody.report_ok = False
    else:

        def report_failed(reason, exit_code):
            custody.reports.append((reason, exit_code))
            raise rt.Failure("custody")

        custody.report_failed = report_failed
    with pytest.raises(rt.Failure) as caught:
        go()
    names = {o["name"] for o in OUTPUTS}
    assert caught.value.code == "entry" and caught.value.reported is False
    assert len(custody.reports) == 2  # both attempts were made, then nothing completes
    assert rt.DIAGNOSTIC_NAME not in custody.parts
    assert custody.done == names - {rt.DIAGNOSTIC_NAME}  # outputs stay incomplete
    assert not [e for e in log if e[:2] == ("chunk", rt.DIAGNOSTIC_NAME)]
