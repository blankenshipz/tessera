"""Isolated setup-script test: real control state, synthetic IPC/HTTPS only, no network."""

import importlib.util
import json
import os
import stat
from pathlib import Path

import pytest

from tessera_control import salad_live_check as live_check
from tessera_control.remote_execution_models import RemoteProposal
from tessera_control.salad_live_check import SaladLiveSession
from tessera_control.salad_safety_service import SafetyHTTPSClient
from tessera_control.salad_server import SaladSafetyClient
from tessera_control.store import Store, _connect, initialize_state

SPEC = importlib.util.spec_from_file_location(
    "salad_live_setup", Path(__file__).parents[1] / "scripts/salad_live_setup.py"
)
setup_script = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup_script)

IMAGE = "ghcr.io/example/tessera-salad-smoke@sha256:" + "1" * 64
GPU = "550e8400-e29b-41d4-a716-446655440000"


def private_file(path, data):
    path.write_bytes(data)
    path.chmod(0o600)
    return path


@pytest.fixture
def inputs(tmp_path):
    root = tmp_path.resolve()
    root.chmod(0o700)
    state = root / "state"
    initialize_state(state)
    credentials = root / "private-credentials"
    credentials.mkdir(mode=0o700)
    token = private_file(root / "custody-token", b"synthetic-admin-token")
    ca = private_file(root / "custody-ca.pem", b"synthetic-ca-not-a-real-certificate")
    output = root / "salad-live"
    argv = [
        "--state-dir",
        str(state),
        "--owner-header",
        str(state / "owner.header"),
        "--output-dir",
        str(output),
        "--image",
        IMAGE,
        "--salad-org",
        "example-org",
        "--salad-project",
        "tessera-research",
        "--gpu-class-id",
        GPU,
        "--country",
        "US",
        "--custody-dns",
        "custody.example-tailnet.ts.net",
        "--custody-admin-host",
        "custody-admin.example-tailnet.ts.net",
        "--custody-ca-file",
        str(ca),
        "--custody-cert-sha256",
        "a" * 64,
        "--custody-token-file",
        str(token),
        "--credential-directory",
        str(credentials),
        "--credential-version",
        "1",
        "--guardian-socket",
        str(root / "guardian" / "guardian.sock"),
        "--guardian-uid",
        str(os.geteuid()),
        "--source-commit",
        "c" * 40,
    ]
    return dict(root=root, state=state, output=output, argv=argv)


def run_setup(inputs, capsys):
    assert setup_script.main(inputs["argv"]) == 0, capsys.readouterr().err
    out = capsys.readouterr().out
    return json.loads(out), out


def open_session(path, monkeypatch):
    # Exactly the synthetic seams concrete_session uses; no IPC/HTTPS is contacted.
    monkeypatch.setattr(SafetyHTTPSClient, "__init__", lambda self, **kw: None)
    monkeypatch.setattr(SaladSafetyClient, "_exchange", lambda self, body: None)
    return SaladLiveSession(path)


def test_setup_creates_approved_route_and_private_config(inputs, capsys, monkeypatch):
    summary, stdout = run_setup(inputs, capsys)
    output = inputs["output"]
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    worker_token = (output / "worker-token").read_text()
    owner = (inputs["state"] / "owner.header").read_text().strip().rsplit(" ", 1)[1]
    assert len({worker_token, owner}) == 2
    for secret in (worker_token, owner, "synthetic-admin-token"):
        assert secret not in stdout
        for path in (output / "journal").iterdir():
            assert secret not in path.read_text()
    for name in ("worker-token", "salad-live-config.json", "setup-summary.json"):
        assert stat.S_IMODE((output / name).stat().st_mode) == 0o600
    assert summary["status"] == "route_approved_not_dispatched"
    review = {item["field"] for item in summary["review_required"]}
    assert {"execution.watchdog_profile", "execution.monitor_profile", "resources"} <= review

    path = output / "salad-live-config.json"
    config = json.loads(path.read_text())
    assert config["approved_max_cost_cents"] == 500
    assert config["max_seconds"] == 300 and config["max_polls"] == 30
    assert config["policy"]["management"] == {
        "reference": "credential-ref:salad-management",
        "version": 1,
        "purpose": "salad_management",
    }
    salad = config["policy"]["target"]["selection"]["salad"]
    execution = salad["execution"]
    route = summary["records"]
    assert (salad["organization"], salad["project"]) == ("example-org", "tessera-research")
    assert config["policy"]["target"]["selection"]["resources"]["gpu_class"] == GPU
    assert execution["argv_binding"] == "smoke_operation_v1"
    assert execution["argv"] == [
        "v1",
        "public-integers-v1",
        route["operation_id"],
        str(route["epoch"]),
        "custody.example-tailnet.ts.net",
        "b" * 64,
        "300",
        "1024",
        "1",
        "4",
    ]
    assert execution["max_cost_cents"] == 500

    store = Store(inputs["state"])
    assert store.authenticate_worker(worker_token)["kind"] == "worker"
    assert not (output / "cancel-token").exists()
    assert "cancel_credential_file" not in config
    with _connect(inputs["state"] / "tessera.sqlite3") as db:
        budget = dict(
            db.execute(
                "SELECT * FROM experiment_budgets WHERE experiment_id=?", (route["experiment_id"],)
            ).fetchone()
        )
        held = db.execute(
            "SELECT held_cents FROM experiment_budget_reservations WHERE admitted_attempt_id=?",
            (route["admitted_attempt_id"],),
        ).fetchone()[0]
    assert budget["ceiling_cents"] == 500 and held == 500
    assert [
        budget[k]
        for k in ("prior_settled_cents", "prior_pending_cents", "ancillary_cents", "shutdown_cents")
    ] == [0, 0, 0, 0]

    # Same pre-dispatch state as test_concrete_private_composition_reconnect_no_dispatch.
    session = open_session(path, monkeypatch)
    try:
        state = session.state(route["route_id"])
        assert state["effects"] == [] and state["approved_by"] and state["desired"] == "run"
        assert state["proposal"]["selection"]["salad"]["group"] == route["salad_group"]
        other = session.reopen()
        assert other.state(route["route_id"])["effects"] == []
        assert other.server is not session.server
        assert other.dispatcher.store is other.server.store
        other.close()
        # Real use-time authority (budget binding, capability, argv, plan outputs)
        # admits create while the lease is current; nothing is dispatched.
        authority = session.server.store.preflight_authority(
            session.server.worker,
            route["route_id"],
            RemoteProposal.model_validate(state["proposal"]),
            "create",
        )
        binding = authority["budget_binding"]
        assert binding["admitted_attempt_id"] == route["admitted_attempt_id"]
        assert binding["max_exposure_cents"] == 500
        plan = authority["runtime_package"]["plan"]
        assert plan["execution_spec"]["output_paths"] == ["output.json"]
        assert plan["budget_cents"] == 500
        assert session.state(route["route_id"])["effects"] == []
    finally:
        session.close()


def test_setup_refuses_existing_output_directory(inputs, capsys):
    inputs["output"].mkdir(mode=0o700)
    assert setup_script.main(inputs["argv"]) == 1
    assert "already exists" in capsys.readouterr().err
    assert list(inputs["output"].iterdir()) == []


def test_setup_rejects_bad_image_before_any_write(inputs, capsys):
    argv = list(inputs["argv"])
    argv[argv.index("--image") + 1] = "ghcr.io/example/smoke:latest"
    assert setup_script.main(argv) == 1
    assert not inputs["output"].exists()
    capsys.readouterr()


def test_long_route_window_keeps_workload_runtime_bounded(inputs, capsys, monkeypatch):
    inputs["argv"] += ["--max-seconds", "900", "--max-polls", "90", "--workload-seconds", "300"]
    summary, _ = run_setup(inputs, capsys)
    config = json.loads(Path(summary["config_file"]).read_text())
    assert (config["max_seconds"], config["max_polls"]) == (900, 90)
    selection = config["policy"]["target"]["selection"]
    assert selection["resources"]["max_seconds"] == 900
    # The workload's own runtime argument stays within the smoke recipe's 300s bound.
    assert selection["salad"]["execution"]["argv"][6] == "300"
    session = open_session(Path(summary["config_file"]), monkeypatch)
    session.close()


def test_route_window_allows_cold_start_up_to_two_hours(inputs, capsys):
    assert (live_check.MAX_ROUTE_SECONDS, live_check.MAX_POLLS) == (7200, 720)
    inputs["argv"] += ["--max-seconds", "7200", "--max-polls", "720", "--workload-seconds", "300"]
    summary, _ = run_setup(inputs, capsys)
    config = json.loads(Path(summary["config_file"]).read_text())
    assert (config["max_seconds"], config["max_polls"]) == (7200, 720)
    assert config["max_seconds"] / config["max_polls"] <= 10  # polls stay <=10 s apart


def test_route_window_above_cap_or_long_workload_rejected(inputs, capsys):
    bad = list(inputs["argv"]) + ["--max-seconds", "7201"]
    assert setup_script.main(bad) == 1
    assert "--max-seconds" in capsys.readouterr().err
    bad = list(inputs["argv"]) + ["--max-seconds", "3600", "--workload-seconds", "301"]
    assert setup_script.main(bad) == 1
    assert "--workload-seconds" in capsys.readouterr().err


def test_workload_runtime_cannot_exceed_route_window(inputs, capsys):
    inputs["argv"] += ["--max-seconds", "120", "--workload-seconds", "300"]
    assert setup_script.main(inputs["argv"]) == 1
    assert "--workload-seconds" in capsys.readouterr().err
    assert not inputs["output"].exists()


@pytest.mark.parametrize(
    "flags,message",
    [
        (["--disk-mb", "30000"], "--disk-mb: storage must be a whole number of GiB"),
        (["--disk-mb", "257024"], "--disk-mb: storage must be 1..250 GiB"),
        (["--memory-mb", "61441"], "--memory-mb: memory must be 1024..61440 MB"),
        (["--memory-mb", "512"], "--memory-mb: memory must be 1024..61440 MB"),
        (["--cpu-millicores", "17000"], "--cpu-millicores: cpu must be 1..16 vCPUs"),
        (["--cpu-millicores", "1500"], "--cpu-millicores: cpu must be a whole number"),
    ],
)
def test_setup_refuses_a_shape_salad_create_rejects_before_any_write(
    inputs, capsys, flags, message
):
    assert setup_script.main(list(inputs["argv"]) + flags) == 1
    assert message in capsys.readouterr().err
    assert not inputs["output"].exists()


def test_setup_accepts_whole_gib_storage_at_the_documented_limits(inputs, capsys):
    inputs["argv"] += ["--disk-mb", "256000", "--memory-mb", "61440", "--cpu-millicores", "16000"]
    summary, _ = run_setup(inputs, capsys)
    config = json.loads(Path(summary["config_file"]).read_text())
    assert config["policy"]["target"]["selection"]["resources"]["disk_mb"] == 256000
