"""deploy-check fails if and only if the service's own start would refuse its configuration.

Each corpus entry is an operator file set. ``deploy-check``'s startup checks run the service's
``--check-config`` in a real subprocess (as a deploy does); the real start runs in-process with
only the bind, the provider key fetch and the serve loop replaced. The two must agree on every
entry (ADR 0214)."""

from __future__ import annotations

import types

import pytest

pytest.importorskip("cryptography")
from test_guardian_startup_check import good_config, write_config
from test_salad_safety_service import provision_tls
from test_serve_check_config import _private

from tessera_control import (
    campaign_providers,
    cli,
    deploy_check_cli,
    exit_codes,
    operator_config_freshness,
    program_budget,
)
from tessera_control import salad_safety_service as guardian
from tessera_control.campaign_providers import PROVIDERS_FILE_SCHEMA
from tessera_control.salad_workload_jwt import SaladWorkloadJWT
from tessera_control.startup_checks import StartupCheck


def _providers(home, name, **extra):
    return _private(home / f"providers-{name}.json", {"schema": PROVIDERS_FILE_SCHEMA, **extra})


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    for name in (
        "TESSERA_REVIEW_DESTINATIONS",
        "TESSERA_REVIEW_TIERS",
        "TESSERA_CODE_REVIEW_GIT_REMOTES",
    ):
        monkeypatch.delenv(name, raising=False)
    yield
    program_budget.reset()
    operator_config_freshness.clear()
    campaign_providers._CONFIGURED.clear()


@pytest.fixture
def bound(monkeypatch):
    """Record the point a real start would bind, instead of binding."""
    reached: list[str] = []
    monkeypatch.setattr(cli.uvicorn, "run", lambda *a, **k: reached.append("serve"))
    monkeypatch.setattr(
        cli, "create_app", lambda *a, **k: types.SimpleNamespace(state=types.SimpleNamespace())
    )
    monkeypatch.setattr("tessera_control.provider_trust.record_at_start", lambda *a, **k: None)
    monkeypatch.setattr(SaladWorkloadJWT, "refresh", lambda self: None)
    monkeypatch.setattr(guardian, "SafetyHTTPServer", lambda *a, **k: object())
    monkeypatch.setattr(guardian, "serve", lambda *a, **k: reached.append("guardian"))
    return reached


@pytest.fixture
def home(tmp_path):
    tmp_path.chmod(0o700)
    for name in ("state", "credentials", "run", "control"):
        (tmp_path / name).mkdir(mode=0o700)
        (tmp_path / name).chmod(0o700)
    provision_tls(tmp_path)
    return tmp_path


def _serve_corpus(home):
    return {
        "serve_ok": [],
        "serve_ok_width": [
            "--campaign-providers",
            str(_providers(home, "width", salad_max_concurrent_routes=2)),
        ],
        "serve_unknown_key": [
            "--campaign-providers",
            str(_providers(home, "unknown", unknwon_key=1)),
        ],
        "serve_bool_width": [
            "--campaign-providers",
            str(_providers(home, "bool", salad_max_concurrent_routes=True)),
        ],
        "serve_trust_malformed": ["--provider-trust", str(_private(home / "t.json", "{"))],
        "serve_budget_bad": [
            "--program-budget",
            str(_private(home / "b.json", {"schema": "wrong"})),
        ],
        "serve_tick": ["--campaign-tick-seconds", "0.01"],
    }


def _guardian_corpus(home):
    base = good_config(home)
    return {
        "guardian_ok": base,
        "guardian_misspelt_cap": {
            **{k: v for k, v in base.items() if k != "total_cap_cents"},
            "total_cap_cent": 100,
        },
        "guardian_bool_frames": {**base, "max_open_billable_frames": True},
        "guardian_unknown_key": {**base, "max_open_frames": 2},
    }


def _deploy_check_fails(check: StartupCheck) -> bool:
    findings = deploy_check_cli.gather(
        state_dir=None,
        executor_configs=[],
        live_configs=[],
        providers_file=None,
        max_backup_bytes=None,
        smoke=False,
        startup_checks=[check],
    )
    startup = [f for f in findings if f.check_id.startswith("startup.")]
    assert len(startup) == 1
    return startup[0].status == "fail"


SERVE_CASES = [
    "serve_ok",
    "serve_ok_width",
    "serve_unknown_key",
    "serve_bool_width",
    "serve_trust_malformed",
    "serve_budget_bad",
    "serve_tick",
]
GUARDIAN_CASES = [
    "guardian_ok",
    "guardian_misspelt_cap",
    "guardian_bool_frames",
    "guardian_unknown_key",
]


@pytest.mark.parametrize("case", SERVE_CASES)
def test_serve_deploy_check_fails_iff_start_refuses(case, home, bound, capsys):
    args = ["--state-dir", str(home / "control"), "--port", "9", *_serve_corpus(home)[case]]
    refused = cli.main(["serve", *args]) == exit_codes.CONFIG_REFUSED
    assert (bound == ["serve"]) is (not refused)
    assert _deploy_check_fails(StartupCheck("serve", tuple(args))) is refused
    assert refused is not case.startswith("serve_ok")


@pytest.mark.parametrize("case", GUARDIAN_CASES)
def test_guardian_deploy_check_fails_iff_start_refuses(case, home, bound, capsys):
    path = write_config(home, _guardian_corpus(home)[case])
    refused = guardian.main(["--run", "--config", path]) == exit_codes.CONFIG_REFUSED
    assert (bound == ["guardian"]) is (not refused)
    assert _deploy_check_fails(StartupCheck("salad-guardian", ("--run", "--config", path))) is (
        refused
    )
    assert refused is (case != "guardian_ok")


@pytest.mark.parametrize("control,frames,status", [(2, 2, "ok"), (3, 2, "fail")])
def test_width_mismatch_between_control_and_guardian(home, control, frames, status):
    providers = _providers(home, "cross", salad_max_concurrent_routes=control)
    guardian_config = write_config(home, {**good_config(home), "max_open_billable_frames": frames})
    findings = deploy_check_cli.gather(
        state_dir=None,
        executor_configs=[],
        live_configs=[],
        providers_file=None,
        max_backup_bytes=None,
        smoke=False,
        startup_checks=[
            StartupCheck(
                "serve",
                (
                    "--state-dir",
                    str(home / "control"),
                    "--port",
                    "9",
                    "--campaign-providers",
                    str(providers),
                ),
            ),
            StartupCheck("salad-guardian", ("--run", "--config", guardian_config)),
        ],
    )
    by_id = {f.check_id: f for f in findings}
    assert by_id["startup.serve.1"].status == "ok"
    assert by_id["startup.salad-guardian.1"].status == "ok"
    assert by_id["startup.route_width"].status == status
    assert by_id["startup.route_width"].detail == {
        "salad_max_concurrent_routes": control,
        "max_open_billable_frames": frames,
    }
