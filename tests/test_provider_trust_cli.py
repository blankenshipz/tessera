"""``tessera admin provider-trust`` and ``tessera serve --provider-trust`` (ADR 0040)."""

from __future__ import annotations

import json
import sqlite3
import sys

import pytest

from tessera_control import cli
from tessera_control.provider_trust import current_policy
from tessera_control.store import initialize_state

DOCUMENT = {
    "schema": "tessera.provider-trust.v1",
    "providers": {
        "salad": {"data_classes": ["public", "private_derived"], "worker_principal_ids": ["w-1"]}
    },
}


def _trust_file(path, document=DOCUMENT, mode=0o600):
    path.write_text(json.dumps(document))
    path.chmod(mode)
    return path


def _serve(monkeypatch, state, *extra):
    captured = {}
    monkeypatch.setattr(
        cli.uvicorn, "run", lambda app, **kwargs: captured.update(app=app, **kwargs)
    )
    monkeypatch.setattr(
        sys, "argv", ["tessera", "serve", "--state-dir", str(state), "--port", "9", *extra]
    )
    return cli.main(), captured


def _audit(state):
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        return [
            (row[0], json.loads(row[1]))
            for row in db.execute(
                "SELECT event_type, payload_json FROM operator_audit_events ORDER BY cursor"
            )
        ]


def test_serve_installs_and_records_the_trust_file(tmp_path, monkeypatch):
    state = tmp_path / "state"
    initialize_state(state)
    path = _trust_file(tmp_path / "trust.json")
    code, captured = _serve(monkeypatch, state, "--provider-trust", str(path))
    assert code == 0
    assert current_policy().source == "file" and current_policy().document == DOCUMENT
    assert captured["app"].state.provider_trust_path == path
    ((event, payload),) = _audit(state)
    assert event == "provider_trust.policy_changed" and payload["actor"] == "deployment_start"
    # A restart with the same file appends nothing.
    _serve(monkeypatch, state, "--provider-trust", str(path))
    assert len(_audit(state)) == 1


def test_serve_without_a_file_records_the_builtin_default(tmp_path, monkeypatch):
    state = tmp_path / "state"
    initialize_state(state)
    code, _ = _serve(monkeypatch, state)
    assert code == 0 and current_policy().source == "builtin"
    ((_, payload),) = _audit(state)
    assert payload["source"] == "builtin"


@pytest.mark.parametrize("problem", ["mode", "malformed", "symlink", "missing"])
def test_serve_exits_2_on_a_refused_file(tmp_path, monkeypatch, capsys, problem):
    state = tmp_path / "state"
    initialize_state(state)
    real = _trust_file(tmp_path / "trust.json", mode=0o666 if problem == "mode" else 0o600)
    path = real
    if problem == "malformed":
        path.write_text("{")
    elif problem == "symlink":
        path = tmp_path / "link.json"
        path.symlink_to(real)
    elif problem == "missing":
        path = tmp_path / "missing.json"
    code, captured = _serve(monkeypatch, state, "--provider-trust", str(path))
    assert code == 2 and not captured
    assert "serve configuration error" in capsys.readouterr().err
    assert current_policy().source == "builtin"


def test_show_and_reload_call_the_admin_routes(tmp_path, capsys):
    header = tmp_path / "owner.header"
    header.write_text("Authorization: Bearer synthetic\n")
    header.chmod(0o600)
    calls = []

    class Transport:
        def __init__(self, origin, authorization):
            pass

        def __call__(self, method, path, body=None, key=None):
            calls.append((method, path, body, key))
            return 200, {"digest": "d" * 64}

    parsed = cli.parser().parse_args(
        ["admin", "provider-trust", "show", "--owner-header", str(header)]
    )
    from tessera_control.provider_trust_cli import run_provider_trust

    assert run_provider_trust(parsed, Transport) == 0
    reload = cli.parser().parse_args(
        [
            "admin",
            "provider-trust",
            "reload",
            "--owner-header",
            str(header),
            "--idempotency-key",
            "k1",
            "--automation-label",
            "ops",
        ]
    )
    assert run_provider_trust(reload, Transport) == 0
    assert calls == [
        ("GET", "/admin/provider-trust", None, None),
        ("POST", "/admin/provider-trust/reload", {}, "k1"),
    ]
    assert json.loads(capsys.readouterr().out.splitlines()[0]) == {"digest": "d" * 64}
    # There is no edit command.
    with pytest.raises(SystemExit):
        cli.parser().parse_args(["admin", "provider-trust", "set"])
