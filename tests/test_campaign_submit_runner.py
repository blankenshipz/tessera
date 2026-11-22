"""SubmitRunner, RunnerJournal and main (synthetic data, fake clock, no real sleeping)."""

from __future__ import annotations

import json
import os

import httpx
import pytest

from tessera_agent.campaign_submit_action import (
    CampaignSubmitAction,
    SubmitPolicy,
    SubmitRefused,
)
from tessera_agent.campaign_submit_runner import (
    JournalInUse,
    RunnerJournal,
    RunnerSettings,
    SubmitRunner,
    main,
)
from tessera_agent.standing_loop import KillSwitch


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class Client:
    def __init__(self, pending=(), fail=None):
        self.pending, self.fail = list(pending), fail
        self.lists = self.submits = 0

    def list_pending(self, project_id):
        self.lists += 1
        if self.fail:
            raise self.fail
        return {"submission_approvals": self.pending}

    def submit(self, approval_id, revision, key):
        self.submits += 1
        raise SubmitRefused("kill_switch_engaged")


def make(tmp_path, client, **overrides):
    stop = overrides.pop("stop", lambda: False)
    clock = Clock()
    marker_dir = tmp_path / "sw"
    marker_dir.mkdir(exist_ok=True)
    marker = marker_dir / "stop"
    settings = RunnerSettings(
        journal_path=tmp_path / "j.jsonl", kill_switch=marker, **{"max_ticks": 3, **overrides}
    )
    journal = RunnerJournal(settings.journal_path, clock)
    kill = KillSwitch(marker)
    runner = SubmitRunner(
        lambda record: CampaignSubmitAction(client, kill, record, SubmitPolicy("p"), environ={}),
        kill,
        journal,
        settings,
        clock,
        clock.sleep,
        stop,
    )
    return runner, journal, clock, marker, settings


def events(journal):
    with open(journal.path) as f:
        return [json.loads(line) for line in f]


def names(journal):
    return [e["event"] for e in events(journal)]


def test_settings_validation(tmp_path):
    for bad in (
        {"interval_seconds": 4},
        {"interval_seconds": 3601},
        {"max_submissions_per_tick": 0},
        {"max_submissions_per_tick": 11},
    ):
        with pytest.raises(ValueError):
            RunnerSettings(tmp_path / "j", tmp_path / "k", **bad)
    assert RunnerSettings(tmp_path / "j", tmp_path / "k").interval_seconds == 30


def test_kill_switch_engaged_never_calls_action_then_resumes(tmp_path):
    client = Client()
    runner, journal, clock, marker, _ = make(tmp_path, client, max_ticks=6)
    marker.write_text("")
    # clear the marker after the 3rd wait
    original = clock.sleep

    def sleep(seconds):
        original(seconds)
        if len(clock.sleeps) >= 90 and marker.exists():
            marker.unlink()

    runner._sleep = sleep
    assert runner.run() == 0
    n = names(journal)
    assert n.count("kill_switch_engaged") == 1 and n.count("kill_switch_cleared") == 1
    assert client.lists >= 1
    assert n.index("kill_switch_engaged") < n.index("kill_switch_cleared") < n.index("tick")


def test_engaged_for_whole_run_makes_no_calls(tmp_path):
    client = Client()
    runner, journal, _, marker, _ = make(tmp_path, client, max_ticks=5)
    marker.write_text("")
    assert runner.run() == 0
    assert client.lists == 0 and client.submits == 0
    assert names(journal).count("kill_switch_engaged") == 1
    assert "tick" not in names(journal)


def test_missing_kill_switch_directory_fails_closed(tmp_path):
    client = Client()
    runner, journal, _, marker, _ = make(tmp_path, client, max_ticks=2)
    marker.parent.rmdir()
    assert runner.run() == 0
    assert client.lists == 0 and "kill_switch_engaged" in names(journal)


def test_failures_back_off_double_cap_halt_exit_3(tmp_path):
    client = Client(fail=RuntimeError("down"))
    runner, journal, clock, _, _ = make(
        tmp_path, client, max_ticks=None, max_consecutive_failures=4, max_backoff_seconds=50
    )
    assert runner.run() == 3
    backoffs = [e for e in events(journal) if e["event"] == "backoff"]
    assert [b["delay_seconds"] for b in backoffs] == [50, 50, 50]
    assert len(backoffs) == 3
    assert names(journal)[-1] == "halted"
    assert sum(clock.sleeps) == pytest.approx(150)


def test_backoff_doubles_below_cap_and_resets_on_success(tmp_path):
    client = Client(fail=RuntimeError("down"))
    runner, journal, clock, _, _ = make(
        tmp_path, client, max_ticks=5, interval_seconds=5, max_backoff_seconds=300
    )
    flips = {"n": 0}
    real = client.list_pending

    def flaky(project_id):
        flips["n"] += 1
        if flips["n"] == 3:
            client.fail = None
        if flips["n"] == 5:
            client.fail = RuntimeError("again")
        return real(project_id)

    client.list_pending = flaky
    runner.run()
    delays = [e["delay_seconds"] for e in events(journal) if e["event"] == "backoff"]
    assert delays[:2] == [10, 20]
    ticks = [e for e in events(journal) if e["event"] == "tick"]
    assert ticks[2]["failed"] is False
    assert delays[2:] == [10]  # reset to the first step after the good tick


def test_list_failed_event_counts_as_failure(tmp_path):
    client = Client(fail=RuntimeError("down"))
    runner, journal, *_ = make(tmp_path, client, max_ticks=1)
    runner.run()
    assert any(e["event"] == "list_failed" for e in events(journal))
    assert [e for e in events(journal) if e["event"] == "tick"][0]["failed"] is True


def test_refusals_are_not_failures(tmp_path):
    client = Client(pending=[{"id": "a1", "revision": 1}])
    runner, journal, _, _, _ = make(tmp_path, client, max_ticks=3)
    assert runner.run() == 0
    n = names(journal)
    assert "backoff" not in n and "halted" not in n
    tick = next(e for e in events(journal) if e["event"] == "tick")
    assert tick["refused"] == 1 and tick["started"] == 0 and tick["failed"] is False


class SubmitClient(Client):
    def __init__(self, pending, outcomes):
        super().__init__(pending)
        self.outcomes = outcomes  # approval_id -> exception or None (success)

    def submit(self, approval_id, revision, key):
        self.submits += 1
        error = self.outcomes[approval_id]
        if error:
            raise error
        return {"status": "ok"}


def test_submit_failed_every_tick_backs_off_halts_exit_3(tmp_path):
    client = SubmitClient([{"id": "a1", "revision": 1}], {"a1": ConnectionError("down")})
    runner, journal, clock, _, _ = make(
        tmp_path,
        client,
        max_ticks=50,  # bound the run so a regression fails instead of hanging
        interval_seconds=5,
        max_consecutive_failures=4,
        max_backoff_seconds=12,
    )
    assert runner.run() == 3
    ev = events(journal)
    assert any(e["event"] == "submit_failed" for e in ev)
    assert [e["delay_seconds"] for e in ev if e["event"] == "backoff"] == [10, 12, 12]
    assert names(journal)[-1] == "halted"
    assert client.submits == 4  # one failed submit per tick, none after the halt


def test_mixed_success_and_submit_failed_is_not_failed(tmp_path):
    client = SubmitClient(
        [{"id": "a1", "revision": 1}, {"id": "a2", "revision": 1}],
        {"a1": ConnectionError("x"), "a2": None},
    )
    runner, journal, *_ = make(tmp_path, client, max_ticks=1)
    assert runner.run() == 0
    n = names(journal)
    assert "submit_failed" in n and "submitted" in n and "backoff" not in n
    assert next(e for e in events(journal) if e["event"] == "tick")["failed"] is False


def test_stop_request_ends_cleanly_with_prompt_slices(tmp_path):
    client = Client()
    state = {"stop": False}
    runner, journal, clock, _, _ = make(
        tmp_path, client, max_ticks=None, stop=lambda: state["stop"]
    )
    base = clock.sleep

    def sleep(seconds):
        base(seconds)
        if len(clock.sleeps) == 3:
            state["stop"] = True

    runner._sleep = sleep
    assert runner.run() == 0
    assert all(s <= 1.0 for s in clock.sleeps) and len(clock.sleeps) == 3
    assert names(journal)[-1] == "runner_stopped"
    assert client.lists == 1


def test_journal_is_private_exclusive_and_rejects_loose_file(tmp_path):
    clock = Clock()
    path = tmp_path / "j.jsonl"
    journal = RunnerJournal(path, clock)
    assert os.stat(path).st_mode & 0o777 == 0o600
    with pytest.raises(JournalInUse):
        RunnerJournal(path, clock)
    journal.close()
    RunnerJournal(path, clock).close()  # reopen of a private file appends
    loose = tmp_path / "loose.jsonl"
    loose.write_text("")
    loose.chmod(0o644)
    with pytest.raises(ValueError):
        RunnerJournal(loose, clock)
    assert loose.stat().st_mode & 0o777 == 0o644  # never chmodded


def test_journal_symlink_is_refused(tmp_path):
    target = tmp_path / "target.jsonl"
    target.write_text("")
    target.chmod(0o600)
    link = tmp_path / "link.jsonl"
    link.symlink_to(target)
    with pytest.raises(OSError):
        RunnerJournal(link, Clock())
    assert target.read_text() == ""


def test_journal_never_contains_the_token(tmp_path):
    from tessera_mcp.client import ControlClient, SubmissionActionClient
    from tessera_mcp.config import Config

    secret = "synthetic-secret-token-value"
    token = tmp_path / "token"
    token.write_text(secret)
    token.chmod(0o600)
    transport = httpx.MockTransport(lambda r: httpx.Response(500, json={"detail": secret}))
    client = SubmissionActionClient(
        ControlClient(Config("http://127.0.0.1:8765", token, "p", "dest"), transport)
    )
    runner, journal, *_ = make(tmp_path, client, max_ticks=2)
    runner.run()
    assert b"list_failed" in open(journal.path, "rb").read()
    assert secret.encode() not in open(journal.path, "rb").read()


def test_main_refuses_with_api_key_and_never_prints_value(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-value")
    code = main(["--journal", str(tmp_path / "j"), "--kill-switch", str(tmp_path / "k")])
    err = capsys.readouterr().err
    assert code == 2 and "ANTHROPIC_API_KEY" in err and "synthetic-value" not in err
    assert not (tmp_path / "j").exists()


def test_main_missing_env_exits_2_and_requires_kill_switch(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    for var in (
        "TESSERA_CONTROL_URL",
        "TESSERA_AGENT_TOKEN_FILE",
        "TESSERA_PROJECT_ID",
        "TESSERA_AGENT_DESTINATION",
    ):
        monkeypatch.delenv(var, raising=False)
    assert main(["--journal", str(tmp_path / "j"), "--kill-switch", str(tmp_path / "k")]) == 2
    assert not (tmp_path / "j").exists()
    with pytest.raises(SystemExit) as caught:
        main(["--journal", str(tmp_path / "j")])
    assert caught.value.code == 2
