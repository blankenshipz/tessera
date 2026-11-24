"""Operator recovery (--reset, --resolve) for the stuck-finding runner; fakes only."""

from __future__ import annotations

import fcntl
import os

import pytest

from tessera_agent.standing_loop import SqliteCallLedger
from tessera_agent.stuck_loop_control import StuckControlError
from tessera_agent.stuck_loop_runner import EXIT_CONFIG, EXIT_HALTED, EXIT_STOPPED
from tests.test_stuck_loop_runner import FID, FakeControl, Run, finding

FID2 = "stuck_" + "2" * 32


class AckScript(FakeControl):
    """Acks succeed until ``fail`` is set, then they are refused (a definite failure)."""

    fail = False

    def call(self, method, path, body=None, key=None):
        if method == "POST" and self.fail:
            self.calls.append((method, path, body, key))
            raise StuckControlError(403)
        return super().call(method, path, body, key)


def halted_run(tmp_path):
    """FID offered and acknowledged, then FID2's ack is refused until the cap halts the loop."""
    control = AckScript([finding()])
    run = Run(tmp_path, control, ack_on_offer=True, base_backoff_seconds=1)
    assert run.main("--once") == EXIT_STOPPED
    control.findings.append(finding(FID2))
    control.fail = True
    assert run.main("--interval-seconds", "1", "--max-ticks", "10") == EXIT_HALTED
    control.calls.clear()
    return run, control


def uncertain_run(tmp_path):
    control = FakeControl([finding()], ack=OSError("boom"))
    run = Run(tmp_path, control, ack_on_offer=True)
    # An ack that may have landed leaves the item uncertain in the ledger.
    from tessera_agent.stuck_loop_action import StuckAckUncertain

    control.ack = StuckAckUncertain()
    assert run.main("--once") == EXIT_STOPPED
    assert run.ledger_state(f"{FID}:1") == "uncertain"
    control.calls.clear()
    return run, control


def rows(run):
    with SqliteCallLedger(run.ledger) as ledger:
        return {i: ledger.state(i) for i in (f"{FID}:1", f"{FID2}:1")}


def test_reset_unhalts_and_keeps_the_once_per_finding_rows(tmp_path, capsys):
    run, control = halted_run(tmp_path)
    before = rows(run)
    assert before[f"{FID}:1"] == "succeeded"
    assert run.main("--once") == EXIT_HALTED
    assert run.main("--reset") == EXIT_STOPPED
    out = capsys.readouterr().out.strip().splitlines()[-1]
    assert out.startswith("reset:") and len(out.splitlines()) == 1
    assert control.calls == []  # no network, no call
    assert rows(run) == before
    (receipt,) = run.events("operator_reset")
    assert receipt["was"] == "halted"
    control.fail = False
    assert run.main("--once") == EXIT_STOPPED
    assert [c[3] for c in control.posts()] != [] and f"{FID}:1" not in str(control.posts())
    assert len(run.events("stuck_offered")) == 2  # FID once before, FID2 now; never FID again


def test_reset_on_a_running_state_is_harmless(tmp_path):
    run = Run(tmp_path, FakeControl([finding()]))
    assert run.main("--once") == EXIT_STOPPED
    assert run.main("--reset") == EXIT_STOPPED
    assert run.main("--max-ticks", "2") == EXIT_STOPPED
    assert len(run.events("stuck_offered")) == 1


def test_resolve_succeeded_stays_closed(tmp_path, capsys):
    run, control = uncertain_run(tmp_path)
    item = f"{FID}:1"
    assert run.main("--resolve", item, "--outcome", "succeeded") == EXIT_STOPPED
    assert capsys.readouterr().out.strip().splitlines()[-1].startswith("resolved:")
    assert control.calls == []
    assert run.ledger_state(item) == "succeeded"
    assert run.events("uncertain_resolved")[-1]["outcome"] == "succeeded"
    assert run.main("--max-ticks", "2") == EXIT_STOPPED
    assert control.posts() == []


def test_resolve_not_started_is_retryable_once(tmp_path):
    run, control = uncertain_run(tmp_path)
    item = f"{FID}:1"
    assert run.main("--resolve", item, "--outcome", "not_started") == EXIT_STOPPED
    assert run.ledger_state(item) == "failed_before_start"
    control.ack = None
    assert run.main("--max-ticks", "3") == EXIT_STOPPED
    assert len(control.posts()) == 1 and run.ledger_state(item) == "succeeded"
    assert len(run.events("stuck_offered")) == 1


def test_a_running_loop_holds_the_lock_and_nothing_changes(tmp_path):
    run, control = halted_run(tmp_path)
    before = rows(run)
    size = run.journal.stat().st_size
    fd = os.open(str(run.journal) + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert run.main("--reset") == EXIT_CONFIG
    finally:
        os.close(fd)
    assert run.journal.stat().st_size == size and rows(run) == before
    assert run.main("--once") == EXIT_HALTED  # still halted


def test_a_held_ledger_lock_refuses(tmp_path):
    run, _ = halted_run(tmp_path)
    size = run.journal.stat().st_size
    with SqliteCallLedger(run.ledger):
        assert run.main("--reset") == EXIT_CONFIG
    assert run.journal.stat().st_size == size
    assert run.main("--once") == EXIT_HALTED


@pytest.mark.parametrize("item", ["nope:1", f"{FID}:1"])
def test_resolve_refuses_unknown_or_not_uncertain_items(tmp_path, item):
    run, control = halted_run(tmp_path)  # FID:1 is succeeded, so not uncertain
    before, size = rows(run), run.journal.stat().st_size
    assert run.main("--resolve", item, "--outcome", "not_started") == EXIT_CONFIG
    assert rows(run) == before and run.journal.stat().st_size == size
    assert control.calls == []


def test_disabled_policy_touches_nothing(tmp_path, capsys):
    run = Run(tmp_path, enabled=False)
    assert run.main("--reset") == EXIT_STOPPED
    assert capsys.readouterr().out.strip() == "disabled" and run.untouched()


def test_refusals_without_kill_switch_key_or_matching_project(tmp_path):
    run = Run(tmp_path)
    assert run.main("--reset", kill=False) == EXIT_CONFIG
    assert run.main("--reset", environ={"ANTHROPIC_API_KEY": "decoy"}) == EXIT_CONFIG
    assert run.main("--reset", environ={"TESSERA_PROJECT_ID": "other"}) == EXIT_CONFIG
    assert run.untouched()


@pytest.mark.parametrize(
    "extra",
    [
        ["--reset", "--once"],
        ["--reset", "--max-ticks", "2"],
        ["--reset", "--resolve", "x:1", "--outcome", "succeeded"],
        ["--resolve", "x:1"],
        ["--outcome", "succeeded"],
        ["--resolve", "x:1", "--outcome", "maybe"],
    ],
)
def test_recovery_flags_are_exclusive_with_ticking_and_each_other(tmp_path, extra):
    run = Run(tmp_path)
    with pytest.raises(SystemExit) as exc:
        run.main(*extra)
    assert exc.value.code == EXIT_CONFIG
    assert run.untouched()
