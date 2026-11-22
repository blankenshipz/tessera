"""Stable executor enrollment: real control state in process, no network."""

import importlib.util
import json
import os
import stat
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tessera_control import executor_enrollment as ee
from tessera_control.remote_execution_models import RemoteProposal
from tessera_control.store import Forbidden, Store

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def _module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


enrollment_cli = _module("executor_enrollment")
import test_salad_live_setup as _setup_tests  # noqa: E402

inputs = _setup_tests.inputs
open_session = _setup_tests.open_session
setup_script = _setup_tests.setup_script


def _enroll(inputs, capsys, *, region="salad-us", days=7, provider="salad", name="executor"):
    directory = inputs["root"] / name
    argv = [
        "enroll",
        "--state-dir",
        str(inputs["state"]),
        "--owner-header",
        str(inputs["state"] / "owner.header"),
        "--executor-dir",
        str(directory),
        "--provider",
        provider,
        "--region",
        region,
        "--days",
        str(days),
    ]
    assert enrollment_cli.main(argv) == 0, capsys.readouterr().err
    stdout = capsys.readouterr().out
    return directory, json.loads(stdout), stdout


def _route(inputs, capsys, name):
    output = inputs["root"] / name
    argv = list(inputs["argv"])
    argv[argv.index("--output-dir") + 1] = str(output)
    argv += ["--executor-dir", str(inputs["root"] / "executor")]
    assert setup_script.main(argv) == 0, capsys.readouterr().err
    return output, json.loads(capsys.readouterr().out)


def test_enrollment_is_private_and_never_prints_the_token(inputs, capsys):
    directory, record, stdout = _enroll(inputs, capsys)
    token = (directory / ee.TOKEN_NAME).read_text().strip()
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    for name in (ee.TOKEN_NAME, ee.RECORD_NAME):
        assert stat.S_IMODE((directory / name).stat().st_mode) == 0o600
    assert token not in stdout
    for path in (directory / "journal").iterdir():
        assert token not in path.read_text()
    assert token not in (directory / ee.RECORD_NAME).read_text()
    assert record["provider"] == "salad" and record["revoked_at"] is None
    assert record["trust_class"] == "trusted-server" and record["region"] == "salad-us"
    enrolled, not_after = (
        datetime.fromisoformat(record[k].replace("Z", "+00:00"))
        for k in ("enrolled_at", "not_after")
    )
    assert not_after - enrolled == timedelta(days=7)
    expires = datetime.fromisoformat(record["credential_expires_at"].replace("Z", "+00:00"))
    assert expires - enrolled <= timedelta(hours=24)
    worker = Store(inputs["state"]).authenticate_worker(token)
    assert worker["id"] == record["worker_principal_id"]
    assert worker["project_id"] == record["project_id"]


def test_two_salad_routes_reuse_one_executor_principal(inputs, capsys, monkeypatch):
    _, record, _ = _enroll(inputs, capsys)
    first, one = _route(inputs, capsys, "route-1")
    second, two = _route(inputs, capsys, "route-2")
    for summary, output in ((one, first), (two, second)):
        records = summary["records"]
        assert records["worker_principal_id"] == record["worker_principal_id"]
        assert records["project_id"] == record["project_id"]
        assert records["worker_registration_id"] == record["registration_id"]
        assert records["executor"]["credential_id"] == record["credential_id"]
        # No per-route worker token: the config points at the executor's token file.
        assert not (output / "worker-token").exists()
        config = json.loads((output / "salad-live-config.json").read_text())
        assert config["worker_credential_file"] == str(inputs["root"] / "executor/worker-token")
    assert one["records"]["route_id"] != two["records"]["route_id"]
    assert one["records"]["operation_id"] != two["records"]["operation_id"]
    # The second route in the shared project is admitted at use time.
    session = open_session(second / "salad-live-config.json", monkeypatch)
    try:
        route_id = two["records"]["route_id"]
        state = session.state(route_id)
        authority = session.server.store.preflight_authority(
            session.server.worker,
            route_id,
            RemoteProposal.model_validate(state["proposal"]),
            "create",
        )
        assert (
            authority["budget_binding"]["admitted_attempt_id"]
            == (two["records"]["admitted_attempt_id"])
        )
        assert session.state(route_id)["effects"] == []
    finally:
        session.close()


def test_route_refuses_mismatched_region_or_revoked_executor(inputs, capsys):
    directory, _, _ = _enroll(inputs, capsys, region="salad-de")
    argv = list(inputs["argv"]) + ["--executor-dir", str(directory)]
    assert setup_script.main(argv) == 1
    assert "labels do not match" in capsys.readouterr().err
    assert not inputs["output"].exists()
    revoke = [
        "revoke",
        "--state-dir",
        str(inputs["state"]),
        "--owner-header",
        str(inputs["state"] / "owner.header"),
        "--executor-dir",
        str(directory),
    ]
    assert enrollment_cli.main(revoke) == 0
    revoked = json.loads(capsys.readouterr().out)
    assert revoked["revoked_at"] is not None
    token = (directory / ee.TOKEN_NAME).read_text().strip()
    with pytest.raises(Forbidden):
        Store(inputs["state"]).authenticate_worker(token)
    assert setup_script.main(argv) == 1
    assert "revoked" in capsys.readouterr().err


def test_renew_keeps_the_principal_and_token(inputs, capsys):
    directory, record, _ = _enroll(inputs, capsys, days=2)
    renew = [
        "renew",
        "--state-dir",
        str(inputs["state"]),
        "--owner-header",
        str(inputs["state"] / "owner.header"),
        "--executor-dir",
        str(directory),
    ]
    assert enrollment_cli.main(renew) == 0, capsys.readouterr().err
    renewed = json.loads(capsys.readouterr().out)
    assert renewed["worker_principal_id"] == record["worker_principal_id"]
    assert renewed["credential_expires_at"] > record["credential_expires_at"]
    assert renewed["credential_expires_at"] <= renewed["not_after"]
    token = (directory / ee.TOKEN_NAME).read_text().strip()
    worker = Store(inputs["state"]).authenticate_worker(token)
    assert worker["id"] == record["worker_principal_id"]
    assert len(list((directory / "operations").iterdir())) == 1


def test_renew_extends_by_at_most_a_day(tmp_path):
    directory = tmp_path / "executor"
    directory.mkdir(mode=0o700)
    token = "t" * 48
    (directory / ee.TOKEN_NAME).write_text(token)
    (directory / ee.TOKEN_NAME).chmod(0o600)
    start = datetime(2026, 11, 20, 12, tzinfo=UTC)
    replies = {
        "executor-project": {"id": "project_1"},
        "executor-credential": {"credential_id": "credential_1", "principal_id": "principal_1"},
        "executor-registration": {"id": "worker_1", "worker_principal_id": "principal_1"},
        "executor-renew": {"principal_id": "principal_1"},
    }
    calls = []

    def http(step, method, path, body=None):
        calls.append((step, method, path, body))
        return replies[step]

    executor = ee.enroll(
        http,
        directory,
        token,
        provider="salad",
        project_name="salad executor",
        approved_build_identifier="tessera-trusted-salad-server-v1",
        approved_build_digest="d" * 64,
        trust_class="trusted-server",
        region="salad-us",
        now=start,
    )
    assert executor.not_after == "2026-11-27T12:00:00Z"
    assert token not in json.dumps(executor.public())
    renewed = ee.renew(http, directory, now=start + timedelta(days=6, hours=12))
    assert renewed.credential_expires_at == "2026-11-27T12:00:00Z"  # capped at not_after
    assert calls[-1][2] == "/credentials/credential_1/renew"
    with pytest.raises(ee.ExecutorError, match="rotate"):
        ee.renew(http, directory, now=start + timedelta(days=6, hours=13))
    with pytest.raises(ee.ExecutorError, match="seven-day"):
        ee.load(
            directory,
            provider="salad",
            needed_until=start + timedelta(days=7, minutes=1),
            now=start + timedelta(days=7),
        )


@pytest.mark.parametrize("case", ["provider", "lapses", "token-mode", "dir-mode", "tampered"])
def test_load_refuses_unsafe_or_unusable_enrollments(tmp_path, case):
    directory = tmp_path / "executor"
    directory.mkdir(mode=0o700)
    token = "t" * 48
    (directory / ee.TOKEN_NAME).write_text(token)
    (directory / ee.TOKEN_NAME).chmod(0o600)
    start = datetime.now(UTC)

    def http(step, method, path, body=None):
        return {
            "executor-project": {"id": "project_1"},
            "executor-credential": {"credential_id": "credential_1", "principal_id": "principal_1"},
            "executor-registration": {"id": "worker_1", "worker_principal_id": "principal_1"},
        }[step]

    ee.enroll(
        http,
        directory,
        token,
        provider="salad",
        project_name="salad executor",
        approved_build_identifier="tessera-trusted-salad-server-v1",
        approved_build_digest="d" * 64,
        trust_class="trusted-server",
        region="salad-us",
        now=start,
    )
    executor, loaded = ee.load(directory, provider="salad", needed_until=start)
    assert loaded == token and executor.worker_principal_id == "principal_1"
    needed = start + timedelta(hours=1)
    provider = "salad"
    if case == "provider":
        provider = "local-docker"
    elif case == "lapses":
        needed = start + timedelta(hours=25)
    elif case == "token-mode":
        (directory / ee.TOKEN_NAME).chmod(0o640)
    elif case == "dir-mode":
        directory.chmod(0o750)
    else:
        record = json.loads((directory / ee.RECORD_NAME).read_text())
        record["not_after"] = (start + timedelta(days=30)).isoformat()
        (directory / ee.RECORD_NAME).write_text(json.dumps(record))
    with pytest.raises(ee.ExecutorError):
        ee.load(directory, provider=provider, needed_until=needed)


def test_enroll_refuses_a_lifetime_above_seven_days(tmp_path):
    directory = tmp_path / "executor"
    directory.mkdir(mode=0o700)
    with pytest.raises(ee.ExecutorError, match="seven days"):
        ee.enroll(
            lambda *a, **k: {},
            directory,
            "t" * 48,
            provider="salad",
            project_name="x",
            approved_build_identifier="tessera-trusted-salad-server-v1",
            approved_build_digest="d" * 64,
            trust_class="trusted-server",
            region="salad-us",
            lifetime=timedelta(days=8),
        )
    assert os.listdir(directory) == []


def test_approved_code_routes_pass_trust_listing_only_the_executor(inputs, capsys):
    """The trust setting names one principal; two approved-code routes reuse it and a
    route that mints its own principal is still refused."""
    from provider_trust_fixture import CODE, install_trust
    from test_gpu_runtime_entry_input import smoke_stage

    _, record, _ = _enroll(inputs, capsys)
    install_trust({"salad": (CODE, [record["worker_principal_id"]])})
    smoke_stage.stage(
        inputs["root"] / "smoke-input",
        inputs["root"] / "smoke-contract.json",
        "custody.example-tailnet.ts.net",
    )
    staged = [
        "--workload-contract",
        str(inputs["root"] / "smoke-contract.json"),
        "--input-dir",
        str(inputs["root"] / "smoke-input"),
        "--data-class",
        "public",
        "--license",
        "CC0-1.0",
        "--source",
        "tessera:workloads/gpu_runtime_v1/smoke/stage.py",
        "--approve-private-code",
        "entry.py",
    ]
    routes = []
    for name in ("approved-1", "approved-2"):
        argv = list(inputs["argv"]) + staged
        argv[argv.index("--output-dir") + 1] = str(inputs["root"] / name)
        argv += ["--executor-dir", str(inputs["root"] / "executor")]
        assert setup_script.main(argv) == 0, capsys.readouterr().err
        routes.append(json.loads(capsys.readouterr().out)["records"])
    assert {r["worker_principal_id"] for r in routes} == {record["worker_principal_id"]}
    argv = list(inputs["argv"]) + staged
    argv[argv.index("--output-dir") + 1] = str(inputs["root"] / "own-principal")
    assert setup_script.main(argv) == 1
    assert "not trusted to receive (private_code_approved)" in capsys.readouterr().err


@pytest.mark.parametrize("unresolved,refused", [(0, False), (3, False), (7, False), (8, True)])
def test_shared_project_limits_add_one_route_and_refuse_at_the_cap(unresolved, refused):
    class Reply:
        def __init__(self, status, body):
            self.status_code, self._body = status, body

        def json(self):
            return self._body

    class Client:
        def get(self, path, headers=None, params=None):
            if path.endswith("/execution-limits"):
                return Reply(200, dict(revision=4, held_cents=150, settled_cents=20))
            grants = [dict(status="cancel_requested")] * unresolved + [dict(status="reconciled")]
            return Reply(200, dict(grants=grants))

    control = type("Control", (), dict(client=Client(), owner_token="o" * 43))()
    if refused:
        with pytest.raises(setup_script.SetupError, match="rotate the executor"):
            setup_script._execution_limits(control, "project_1", 50, True)
        return
    body = setup_script._execution_limits(control, "project_1", 50, True)
    assert body == dict(
        expected_revision=4,
        currency="USD",
        monetary_limit_cents=220,
        max_concurrency=unresolved + 1,
    )
    assert setup_script._execution_limits(control, "project_1", 50, False) == dict(
        expected_revision=0, currency="USD", monetary_limit_cents=50, max_concurrency=1
    )


def _grants(inputs):
    import sqlite3

    with sqlite3.connect(inputs["state"] / "tessera.sqlite3") as db:
        return db.execute("SELECT count(*) FROM execution_grants").fetchone()[0]


@pytest.mark.parametrize("case", ["relative", "symlinked-token", "symlinked-dir"])
def test_route_refuses_unsafe_executor_paths_before_any_write(inputs, capsys, case):
    directory, _, _ = _enroll(inputs, capsys)
    target = directory
    if case == "relative":
        target = Path(os.path.relpath(directory))
    elif case == "symlinked-dir":
        target = inputs["root"] / "executor-link"
        target.symlink_to(directory)
    else:
        token = directory / ee.TOKEN_NAME
        moved = inputs["root"] / "moved-token"
        token.rename(moved)
        token.symlink_to(moved)
    argv = list(inputs["argv"]) + ["--executor-dir", str(target)]
    assert setup_script.main(argv) == 1
    assert "executor" in capsys.readouterr().err
    assert not inputs["output"].exists() and _grants(inputs) == 0


def test_route_refuses_a_record_that_drifted_from_control(inputs, capsys):
    """executor.json still looks live but control revoked the credential: refuse before
    any write, never leave a grant in the shared project."""
    directory, _, _ = _enroll(inputs, capsys)
    saved = (directory / ee.RECORD_NAME).read_bytes()
    revoke = [
        "revoke",
        "--state-dir",
        str(inputs["state"]),
        "--owner-header",
        str(inputs["state"] / "owner.header"),
        "--executor-dir",
        str(directory),
    ]
    assert enrollment_cli.main(revoke) == 0
    capsys.readouterr()
    (directory / ee.RECORD_NAME).write_bytes(saved)  # stale record, revoked at control
    argv = list(inputs["argv"]) + ["--executor-dir", str(directory)]
    assert setup_script.main(argv) == 1
    assert "not live at control" in capsys.readouterr().err
    assert not inputs["output"].exists() and _grants(inputs) == 0


def test_cli_operations_validate_the_directory_before_writing(inputs, capsys):
    directory, _, _ = _enroll(inputs, capsys)
    directory.chmod(0o750)
    renew = [
        "renew",
        "--state-dir",
        str(inputs["state"]),
        "--owner-header",
        str(inputs["state"] / "owner.header"),
        "--executor-dir",
        str(directory),
    ]
    assert enrollment_cli.main(renew) == 1
    assert "0700" in capsys.readouterr().err
    assert not (directory / "operations").exists()


def test_control_refuses_worker_renewal_past_the_total_lifetime(inputs, capsys):
    """Fail-closed at control for every renewer, not only the enrollment tool: a genuinely
    aged credential (issued 6 d 23 h ago, recently renewed) cannot pass issue + 7 d."""
    import sqlite3

    from fastapi.testclient import TestClient

    from tessera_control.api import create_app

    _, record, _ = _enroll(inputs, capsys)
    owner = (inputs["state"] / "owner.header").read_text().strip().split(": ", 1)[1]

    def stamp(delta):
        return (datetime.now(UTC) + delta).isoformat().replace("+00:00", "Z")

    expires = stamp(timedelta(minutes=30))
    with sqlite3.connect(inputs["state"] / "tessera.sqlite3") as db:
        db.execute(
            "UPDATE credentials SET created_at=?, expires_at=? WHERE id=?",
            (stamp(-timedelta(days=6, hours=23)), expires, record["credential_id"]),
        )
        db.execute(
            "INSERT INTO worker_credential_renewals VALUES (?,?,?,?,?)",
            ("renewal_aged", record["credential_id"], stamp(-timedelta(hours=1)), expires, "p"),
        )
    client = TestClient(create_app(inputs["state"]))

    def renew(delta, key):
        return client.post(
            f"/credentials/{record['credential_id']}/renew",
            json={"expires_at": stamp(delta)},
            headers={"Authorization": owner, "Idempotency-Key": key},
        )

    refused = renew(timedelta(hours=23), "renew-past-bound")
    assert refused.status_code == 409 and "seven days" in refused.json()["detail"]
    assert renew(timedelta(minutes=50), "renew-within-bound").status_code == 200


def test_route_refuses_when_control_expiry_passes_the_enrollment_bound(inputs, capsys):
    """executor.json claims a short bound but control's credential runs past it (renewed
    out of band): checked against control's rows, refused before any write."""
    directory, _, _ = _enroll(inputs, capsys)
    record = json.loads((directory / ee.RECORD_NAME).read_text())
    bound = (datetime.now(UTC) + timedelta(hours=3)).isoformat().replace("+00:00", "Z")
    record.update(not_after=bound, credential_expires_at=bound)
    (directory / ee.RECORD_NAME).write_text(json.dumps(record))
    argv = list(inputs["argv"]) + ["--executor-dir", str(directory)]
    assert setup_script.main(argv) == 1
    assert "seven-day bound at control" in capsys.readouterr().err
    assert not inputs["output"].exists() and _grants(inputs) == 0


def test_route_setup_leases_after_the_executor_registry_sighting_went_stale(inputs, capsys):
    """Enrollment registers the worker once; a route set up minutes later must report the
    worker's liveness itself before leasing (60 s disconnect bound)."""
    import sqlite3

    _, record, _ = _enroll(inputs, capsys)
    stale = (datetime.now(UTC) - timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
    with sqlite3.connect(inputs["state"] / "tessera.sqlite3") as db:
        db.execute(
            "UPDATE worker_registry SET last_seen_at=?, heartbeat_at=? WHERE worker_principal_id=?",
            (stale, stale, record["worker_principal_id"]),
        )
    _, summary = _route(inputs, capsys, "stale-route")
    assert summary["records"]["lease_id"]


def test_authentication_refuses_a_worker_credential_past_seven_days_at_use_time(inputs, capsys):
    """Defense in depth: even with a valid-looking recent renewal (which satisfies the
    24 h renewal window), a worker credential issued more than seven days ago fails."""
    import sqlite3

    directory, record, _ = _enroll(inputs, capsys)
    token = (directory / ee.TOKEN_NAME).read_text().strip()
    store = Store(inputs["state"])
    assert store.authenticate_worker(token)["id"] == record["worker_principal_id"]

    def stamp(delta):
        return (datetime.now(UTC) + delta).isoformat().replace("+00:00", "Z")

    renewed, expires = stamp(-timedelta(hours=1)), stamp(timedelta(hours=20))
    with sqlite3.connect(inputs["state"] / "tessera.sqlite3") as db:
        db.execute(
            "UPDATE credentials SET created_at=?, expires_at=? WHERE id=?",
            (stamp(-timedelta(days=8)), expires, record["credential_id"]),
        )
        db.execute(
            "INSERT INTO worker_credential_renewals VALUES (?,?,?,?,?)",
            ("renewal_test", record["credential_id"], renewed, expires, "principal_test"),
        )
    with pytest.raises(Forbidden):
        store.authenticate_worker(token)


# -- enrolling into an existing project (ADR 0056) -------------------------------------------


def _enroll_into(inputs, capsys, project_id, *, name, provider, region="local", days=7):
    directory = inputs["root"] / name
    argv = [
        "enroll",
        "--state-dir",
        str(inputs["state"]),
        "--owner-header",
        str(inputs["state"] / "owner.header"),
        "--executor-dir",
        str(directory),
        "--provider",
        provider,
        "--region",
        region,
        "--days",
        str(days),
        "--existing-project",
        project_id,
    ]
    code = enrollment_cli.main(argv)
    captured = capsys.readouterr()
    return code, directory, captured


def _count(inputs, table):
    from tessera_control.store import _connect

    with _connect(inputs["state"] / "tessera.sqlite3") as connection:
        return connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def test_a_cpu_worker_and_a_salad_executor_share_one_existing_project(inputs, capsys):
    cpu_dir, cpu, _ = _enroll(inputs, capsys, provider="local-docker", region="local", name="cpu")
    projects = _count(inputs, "projects")
    code, salad_dir, out = _enroll_into(
        inputs, capsys, cpu["project_id"], name="salad", provider="salad", region="salad-us"
    )
    assert code == 0, out.err
    salad = json.loads(out.out)
    assert salad["project_id"] == cpu["project_id"] and salad["project_existing"] is True
    assert salad["worker_principal_id"] != cpu["worker_principal_id"]
    assert salad["provider"] == "salad" and salad["trust_class"] == "trusted-server"
    assert _count(inputs, "projects") == projects  # no project was created
    # Both worker principals live in the one project and can authenticate there.
    store = Store(inputs["state"])
    for directory, record in ((cpu_dir, cpu), (salad_dir, salad)):
        token = (directory / ee.TOKEN_NAME).read_text().strip()
        worker = store.authenticate_worker(token)
        assert worker["project_id"] == cpu["project_id"]
        assert worker["id"] == record["worker_principal_id"]
        assert token not in out.out and token not in out.err
        assert token not in json.dumps(record)
    assert ee.read(salad_dir).project_existing is True
    assert ee.read(cpu_dir).project_existing is False


def test_enrolling_into_an_existing_project_is_journaled_and_private(inputs, capsys):
    _, cpu, _ = _enroll(inputs, capsys, provider="local-docker", region="local", name="cpu")
    code, directory, out = _enroll_into(
        inputs, capsys, cpu["project_id"], name="salad", provider="salad", region="salad-us"
    )
    assert code == 0, out.err
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    steps = sorted(path.name for path in (directory / "journal").iterdir())
    assert any("executor-project-existing" in name for name in steps)
    assert not any(name.endswith("executor-project.request.json") for name in steps)
    token = (directory / ee.TOKEN_NAME).read_text().strip()
    for path in (directory / "journal").iterdir():
        assert token not in path.read_text()


def test_the_same_existing_project_enrollment_is_idempotent(inputs, capsys):
    _, cpu, _ = _enroll(inputs, capsys, provider="local-docker", region="local", name="cpu")
    code, directory, first = _enroll_into(
        inputs, capsys, cpu["project_id"], name="salad", provider="salad", region="salad-us"
    )
    assert code == 0
    credentials, principals = _count(inputs, "credentials"), _count(inputs, "principals")
    token = (directory / ee.TOKEN_NAME).read_text()
    code, _, second = _enroll_into(
        inputs, capsys, cpu["project_id"], name="salad", provider="salad", region="salad-us"
    )
    assert code == 0, second.err
    assert json.loads(second.out) == json.loads(first.out)
    assert (_count(inputs, "credentials"), _count(inputs, "principals")) == (
        credentials,
        principals,
    )
    assert (directory / ee.TOKEN_NAME).read_text() == token  # nothing was rewritten


def test_a_directory_holding_another_enrollment_is_refused_not_overwritten(inputs, capsys):
    _, cpu, _ = _enroll(inputs, capsys, provider="local-docker", region="local", name="cpu")
    code, directory, _ = _enroll_into(
        inputs, capsys, cpu["project_id"], name="salad", provider="salad", region="salad-us"
    )
    assert code == 0
    before = (directory / ee.RECORD_NAME).read_text()
    for provider, project in (("self-hosted", cpu["project_id"]), ("salad", "project_" + "a" * 32)):
        code, _, out = _enroll_into(
            inputs, capsys, project, name="salad", provider=provider, region="salad-us"
        )
        assert code == 1 and "different enrollment" in out.err
    assert (directory / ee.RECORD_NAME).read_text() == before


@pytest.mark.parametrize("project", ["project_" + "0" * 32, "not-a-project", "project_x"])
def test_an_unknown_or_malformed_project_is_refused_before_anything_is_issued(
    inputs, capsys, project
):
    credentials, principals = _count(inputs, "credentials"), _count(inputs, "principals")
    code, directory, out = _enroll_into(
        inputs, capsys, project, name="salad", provider="salad", region="salad-us"
    )
    assert code == 1
    assert "existing project" in out.err
    assert (_count(inputs, "credentials"), _count(inputs, "principals")) == (
        credentials,
        principals,
    )
    assert not (directory / ee.RECORD_NAME).exists()


def test_a_project_name_cannot_be_combined_with_an_existing_project(inputs, capsys):
    _, cpu, _ = _enroll(inputs, capsys, provider="local-docker", region="local", name="cpu")
    argv = [
        "enroll",
        "--state-dir",
        str(inputs["state"]),
        "--owner-header",
        str(inputs["state"] / "owner.header"),
        "--executor-dir",
        str(inputs["root"] / "both"),
        "--provider",
        "salad",
        "--region",
        "salad-us",
        "--existing-project",
        cpu["project_id"],
        "--project-name",
        "other",
    ]
    assert enrollment_cli.main(argv) == 1
    assert "cannot be used with" in capsys.readouterr().err


def test_a_worker_credential_cannot_enroll_into_a_project(inputs, capsys, tmp_path):
    """Control issues credentials to the local owner only; a worker token is refused."""
    directory, cpu, _ = _enroll(inputs, capsys, provider="local-docker", region="local", name="cpu")
    header = inputs["root"] / "worker.header"
    token = (directory / ee.TOKEN_NAME).read_text().strip()
    header.write_text(f"Authorization: Bearer {token}\n")
    header.chmod(0o600)
    argv = [
        "enroll",
        "--state-dir",
        str(inputs["state"]),
        "--owner-header",
        str(header),
        "--executor-dir",
        str(inputs["root"] / "intruder"),
        "--provider",
        "salad",
        "--region",
        "salad-us",
        "--existing-project",
        cpu["project_id"],
    ]
    assert enrollment_cli.main(argv) == 1
    assert "stopped" in capsys.readouterr().err
    assert not (inputs["root"] / "intruder" / ee.RECORD_NAME).exists()


def test_renew_and_revoke_work_for_an_executor_in_an_existing_project(inputs, capsys):
    _, cpu, _ = _enroll(inputs, capsys, provider="local-docker", region="local", name="cpu")
    code, directory, _ = _enroll_into(
        inputs, capsys, cpu["project_id"], name="salad", provider="salad", region="salad-us"
    )
    assert code == 0
    base = [
        "--state-dir",
        str(inputs["state"]),
        "--owner-header",
        str(inputs["state"] / "owner.header"),
        "--executor-dir",
        str(directory),
    ]
    assert enrollment_cli.main(["revoke", *base]) == 0
    record = json.loads(capsys.readouterr().out)
    assert record["revoked_at"] and record["project_existing"] is True
    assert record["project_id"] == cpu["project_id"]


def test_a_record_written_before_existing_projects_still_validates(inputs, capsys):
    directory, record, _ = _enroll(inputs, capsys, provider="local-docker", region="local")
    stored = json.loads((directory / ee.RECORD_NAME).read_text())
    stored.pop("project_existing", None)
    (directory / ee.RECORD_NAME).write_text(json.dumps(stored))
    (directory / ee.RECORD_NAME).chmod(0o600)
    assert ee.read(directory).project_existing is False
    stored["project_existing"] = "yes"
    (directory / ee.RECORD_NAME).write_text(json.dumps(stored))
    with pytest.raises(ee.ExecutorError):
        ee.read(directory)
