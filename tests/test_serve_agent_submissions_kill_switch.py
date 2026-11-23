"""`serve --agent-submissions` requires a kill-switch marker path (README, ADR 0093)."""

import sys
import types
from contextlib import nullcontext

from tessera_control import cli
from tessera_control.campaign_submissions import AgentSubmissionConfig, kill_switch_engaged

MESSAGE = "serve configuration error: --agent-submissions needs --agent-submission-kill-switch"


def _serve(monkeypatch, *flags):
    built = []

    def fake_create_app(state_dir, deployment=None, **kwargs):
        app = types.SimpleNamespace(state=types.SimpleNamespace())
        built.append(app)
        return app

    monkeypatch.setattr(cli, "create_app", fake_create_app)
    monkeypatch.setattr("tessera_control.provider_trust.record_at_start", lambda *a, **k: None)
    ran = []
    monkeypatch.setattr(cli.uvicorn, "run", lambda *a, **k: ran.append(True))
    monkeypatch.setattr(cli, "require_sqlite_runtime", lambda: None)
    monkeypatch.setattr(cli.maintenance_lock, "shared", lambda state_dir: nullcontext())
    monkeypatch.setattr(
        sys, "argv", ["tessera", "serve", "--state-dir", "/x", "--port", "9", *flags]
    )
    return cli.main(), built, ran


def test_agent_submissions_without_kill_switch_exits_2_and_starts_nothing(monkeypatch, capsys):
    code, built, ran = _serve(monkeypatch, "--agent-submissions")
    assert code == 2
    assert MESSAGE in capsys.readouterr().err
    assert built == [] and ran == []


def test_kill_switch_given_passes_that_check(monkeypatch, capsys, tmp_path):
    marker = tmp_path / "stop-agent-submissions"
    code, built, ran = _serve(
        monkeypatch, "--agent-submissions", "--agent-submission-kill-switch", str(marker)
    )
    assert code == 0
    assert MESSAGE not in capsys.readouterr().err
    config = built[0].state.agent_submission
    assert config.enabled is True and config.kill_switch_path == str(marker)
    assert ran == [True]


def test_existing_marker_file_engages_the_kill_switch(tmp_path):
    marker = tmp_path / "stop"
    config = AgentSubmissionConfig(enabled=True, kill_switch_path=str(marker))
    assert kill_switch_engaged(config) is False
    marker.write_text("")
    assert kill_switch_engaged(config) is True
