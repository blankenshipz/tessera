"""``tessera serve --check-config`` refuses exactly what a real start refuses, and does no work."""

from __future__ import annotations

import json
import os
import types
from contextlib import nullcontext

import pytest

from tessera_control import (
    campaign_providers,
    cli,
    exit_codes,
    operator_config_freshness,
    program_budget,
)
from tessera_control.campaign_providers import PROVIDERS_FILE_SCHEMA


def _private(path, value):
    text = value if isinstance(value, str) else json.dumps(value)
    path.write_text(text)
    os.chmod(path, 0o600)
    return path


def _providers(tmp_path, **extra):
    return _private(tmp_path / "providers.json", {"schema": PROVIDERS_FILE_SCHEMA, **extra})


@pytest.fixture(autouse=True)
def isolated_process_state():
    yield
    program_budget.reset()
    operator_config_freshness.clear()
    campaign_providers._CONFIGURED.clear()


@pytest.fixture
def state(tmp_path):
    path = tmp_path / "state"
    path.mkdir()
    return path


@pytest.fixture
def no_bind(monkeypatch):
    reached = []

    def fail(*args, **kwargs):
        reached.append(True)
        raise AssertionError("uvicorn.run must not be reached")

    def fake_create_app(*args, **kwargs):
        reached.append("create_app")
        return types.SimpleNamespace(state=types.SimpleNamespace())

    monkeypatch.setattr(cli.uvicorn, "run", fail)
    monkeypatch.setattr(cli, "create_app", fake_create_app)
    monkeypatch.setattr(cli.maintenance_lock, "shared", lambda state_dir: nullcontext())
    monkeypatch.setattr("tessera_control.provider_trust.record_at_start", lambda *a, **k: None)
    for name in ("TESSERA_REVIEW_DESTINATIONS", "TESSERA_REVIEW_TIERS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("TESSERA_CODE_REVIEW_GIT_REMOTES", raising=False)
    return reached


def _bad_cases(tmp_path):
    return {
        "providers_unknown_key": ["--campaign-providers", str(_providers(tmp_path, bogus=1))],
        "providers_bool_width": [
            "--campaign-providers",
            str(_providers(tmp_path, salad_max_concurrent_routes=True)),
        ],
        "providers_width_out_of_range": [
            "--campaign-providers",
            str(_providers(tmp_path, salad_max_concurrent_routes=9)),
        ],
        "trust_malformed": [
            "--provider-trust",
            str(_private(tmp_path / "trust.json", "{not json")),
        ],
        "budget_bad": [
            "--program-budget",
            str(_private(tmp_path / "budget.json", {"schema": "wrong"})),
        ],
        "destinations_malformed": [
            "--review-destinations",
            str(_private(tmp_path / "dest.json", "[]")),
        ],
        "tick_out_of_range": ["--campaign-tick-seconds", "0.01"],
    }


CASES = [
    "providers_unknown_key",
    "providers_bool_width",
    "providers_width_out_of_range",
    "trust_malformed",
    "budget_bad",
    "destinations_malformed",
    "tick_out_of_range",
]


@pytest.mark.parametrize("case", CASES)
def test_bad_file_exits_2_in_check_and_in_real_start(case, tmp_path, state, no_bind, capsys):
    flags = _bad_cases(tmp_path)[case]
    base = ["serve", "--state-dir", str(state), "--port", "9", *flags]
    before = sorted(p.name for p in state.iterdir())
    checked = cli.main([*base, "--check-config"])
    check_err = capsys.readouterr()
    started = cli.main(base)
    start_err = capsys.readouterr()
    assert checked == exit_codes.CONFIG_REFUSED
    assert started == exit_codes.CONFIG_REFUSED
    assert check_err.out == ""
    assert check_err.err == start_err.err and check_err.err
    assert no_bind == []
    assert sorted(p.name for p in state.iterdir()) == before


def test_valid_set_checks_ok_prints_width_and_touches_nothing(tmp_path, state, no_bind, capsys):
    providers = _providers(tmp_path, salad_max_concurrent_routes=2)
    budget_free = ["--campaign-providers", str(providers)]
    before = sorted(p.name for p in tmp_path.rglob("*"))
    code = cli.main(
        ["serve", "--state-dir", str(state), "--port", "9", *budget_free, "--check-config"]
    )
    out = capsys.readouterr()
    assert code == exit_codes.OK
    lines = out.out.splitlines()
    assert len(lines) == 1
    document = json.loads(lines[0])
    assert document["schema"] == exit_codes.CONFIG_CHECK_SCHEMA
    assert document["program"] == "serve" and document["status"] == "ok"
    assert document["widths"] == {"salad_max_concurrent_routes": 2}
    assert isinstance(document["notes"], list)
    assert list(state.iterdir()) == []
    assert sorted(p.name for p in tmp_path.rglob("*")) == before
    assert no_bind == []


def test_valid_set_without_width_reports_null(tmp_path, state, no_bind, capsys):
    code = cli.main(["serve", "--state-dir", str(state), "--port", "9", "--check-config"])
    assert code == exit_codes.OK
    assert json.loads(capsys.readouterr().out)["widths"] == {"salad_max_concurrent_routes": None}


def test_runtime_owner_config_is_noted_not_exercised(tmp_path, state, no_bind, capsys):
    owner = _private(tmp_path / "owner.json", {})
    code = cli.main(
        [
            "serve",
            "--state-dir",
            str(state),
            "--port",
            "9",
            "--runtime-owner-config",
            str(owner),
            "--runtime-owner-sha256",
            "0" * 64,
            "--check-config",
        ]
    )
    assert code == exit_codes.OK
    assert "runtime owner config not exercised" in json.loads(capsys.readouterr().out)["notes"]


def test_check_failure_other_than_refusal_exits_1(tmp_path, state, no_bind, monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise OSError("disk")

    monkeypatch.setattr("tessera_control.campaign_providers.configure_providers", boom)
    code = cli.main(["serve", "--state-dir", str(state), "--port", "9", "--check-config"])
    assert code == exit_codes.RUNTIME_FAILURE
    assert capsys.readouterr().out == ""
