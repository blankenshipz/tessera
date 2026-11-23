"""Reasoning installer: plan/apply, idempotence, safety. Unprivileged: tmp_path is the root."""

import json
import os
import re
import stat
from dataclasses import replace
from pathlib import Path

import pytest

pytest.importorskip("mcp")  # the real profile validator imports it

from tessera_agent import reasoning_install as ri
from tessera_agent.reasoning_install import InstallRefused, Options

SENTINEL_MODEL = "claude-sonnet-sentinelmodel"
SENTINEL_REF = "anthropic-api-key://sentineowner/sentinelref"
REPO = Path(__file__).resolve().parent.parent


class FakeAccounts:
    def __init__(self, existing=False):
        self.users = {"tessera-reasoning": (990, 991)} if existing else {}
        self.created, self.chowns, self.owners = [], [], {}

    def lookup(self, user):
        return self.users.get(user)

    def create(self, user):
        self.created.append(user)
        self.users[user] = (990, 991)

    def chown(self, path, uid, gid):
        path = Path(path)
        self.chowns.append((path, uid, gid))
        match = re.fullmatch(r"\.(.+)\.[0-9a-f]{16}\.tmp", path.name)
        if match:  # a staged temp file is renamed onto its final name afterwards
            path = path.with_name(match.group(1))
        self.owners[path] = (uid, gid)

    def owner(self, path):  # optional hook: simulated ownership, as real chown cannot run here
        return self.owners.get(Path(path), (os.getuid(), os.getgid()))


class FakeRunner:
    def __init__(self):
        self.calls = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        return 0


def readiness_fake(overall="ready_except_key"):
    calls = []

    def check(**kwargs):
        calls.append(kwargs)
        return {"overall": overall, "checks": []}

    check.calls = calls
    return check


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    src = tmp_path / "src"
    src.mkdir()
    (src / "prompt.txt").write_text("synthetic prompt\n")
    template = {
        "run_id": "run1",
        "agent": "researcher",
        "grant_id": "grant1",
        "subject_credential_id": "agent1",
        "destination": "lab",
        "account_mode": "anthropic-api-key-ref",
        "credential_ref": SENTINEL_REF,
        "model": {"providerID": "anthropic", "id": SENTINEL_MODEL},
        "max_steps": 3,
        "max_tokens": 100,
        "max_duration_seconds": 60,
        "permissions": [{"action": "*", "resource": "*", "effect": "deny"}],
        "limits": {"max_output_tokens_per_call": 64, "max_input_tokens_per_call": 1000},
        "pricing_cents_per_mtok": {
            "input": 300,
            "output": 1500,
            "cache_read": 30,
            "cache_write": 375,
        },
        "agent_token_env": "TESSERA_REASONING_AGENT_TOKEN",
    }
    (src / "template.json").write_text(json.dumps(template))
    options = Options(
        root=root,
        profile_template=src / "template.json",
        prompt_source=src / "prompt.txt",
        max_cost_cents_per_call=10,
        daily_cost_cap_cents=100,
        daily_token_cap=10000,
    )
    return options, src


def real(options, path):
    return Path(options.root) / str(path).lstrip("/")


def install_interpreter(options):
    """The interpreter is the operator's (Tessera is installed there first): stage a stand-in."""
    path = real(options, options.python)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)


def snapshot(root):
    out = {}
    for path in sorted(Path(root).rglob("*")):
        info = path.lstat()
        out[str(path)] = (info.st_ino, info.st_mtime_ns, stat.S_IMODE(info.st_mode))
    return out


def do_apply(options, accounts=None, runner=None, ready=None):
    accounts = accounts or FakeAccounts()
    runner = runner or FakeRunner()
    ready = ready or readiness_fake()
    return ri.apply(options, accounts=accounts, runner=runner, readiness=ready), accounts, runner


def test_plan_on_empty_root_is_all_missing_and_writes_nothing(env):
    options, _ = env
    before = snapshot(options.root)
    steps = ri.plan(options, accounts=FakeAccounts())
    assert [s.name for s in steps] == [
        "service_user",
        "config_dir",
        "credentials_dir",
        "state_dir",
        "prompt",
        "profile",
        "model_credential_reference",
        "interpreter",
        "systemd_unit",
    ]
    assert {s.state for s in steps} == {"missing"}
    assert snapshot(options.root) == before == {}


def test_apply_creates_the_exact_tree_with_modes_and_owners(env):
    options, _ = env
    result, accounts, runner = do_apply(options, ready=readiness_fake("ready_except_key"))
    expected = {
        "etc/tessera": (0o755, (0, 0)),
        "etc/tessera/credentials": (0o700, (0, 0)),
        "var/lib/tessera-reasoning": (0o700, (990, 991)),
        "etc/tessera/reasoning-prompt.txt": (0o600, (0, 0)),
        "etc/tessera/reasoning.json": (0o600, (0, 0)),
        "etc/tessera/credentials/reasoning-model-credential": (0o600, (0, 0)),
        "etc/systemd/system/tessera-reasoning.service": (0o644, (0, 0)),
    }
    for rel, (mode, owner) in expected.items():
        path = Path(options.root) / rel
        assert stat.S_IMODE(path.lstat().st_mode) == mode, rel
        assert accounts.owners[path] == owner, rel
    # only the managed names exist (no key, token, admission, temp leftovers)
    names = {str(p.relative_to(options.root)) for p in Path(options.root).rglob("*") if p.is_file()}
    assert names == {
        rel for rel in expected if not rel.endswith(("tessera", "credentials", "reasoning"))
    }
    assert (
        Path(options.root) / "etc/tessera/credentials/reasoning-model-credential"
    ).read_text() == (SENTINEL_REF + "\n")
    assert accounts.created == ["tessera-reasoning"]
    assert runner.calls == [["systemctl", "daemon-reload"]]
    assert result["exit_code"] == 10
    assert result["readiness"]["overall"] == "ready_except_key"
    assert [s["name"] for s in result["steps"]][0] == "service_user"
    assert {s["name"] for s in result["pending_operator"]} == {
        "api_key",
        "agent_token",
        "spend_limits",
        "grant_and_agent_credential",
        "admission",
    }


def test_readiness_receives_real_paths_and_passes_through(env):
    options, _ = env
    options = replace(options, control_url="http://control.invalid", project_id="p1")
    ready = readiness_fake("not_ready")
    result, _, _ = do_apply(options, ready=ready)
    assert result["exit_code"] == 1
    kwargs = ready.calls[0]
    assert kwargs["profile_path"] == real(options, options.profile_path)
    assert kwargs["credentials_directory"] == real(options, options.credentials_dir)
    assert kwargs["state_dir"] == real(options, options.state_dir)
    assert kwargs["control_url"] == "http://control.invalid" and kwargs["project_id"] == "p1"
    for overall, code in (("ready", 0), ("not_fully_checked", 11)):
        assert do_apply(options, ready=readiness_fake(overall))[0]["exit_code"] == code


def test_second_apply_is_a_no_op_and_plan_shows_present(env):
    options, _ = env
    _, accounts, _ = do_apply(options)
    install_interpreter(options)
    before = snapshot(options.root)
    runner = FakeRunner()
    chowns = len(accounts.chowns)
    result, _, _ = do_apply(options, accounts=accounts, runner=runner)
    assert snapshot(options.root) == before
    assert runner.calls == [] and len(accounts.chowns) == chowns
    assert accounts.created == ["tessera-reasoning"]
    assert {s["result"] for s in result["steps"]} == {"unchanged"}
    assert {s.state for s in ri.plan(options, accounts=accounts)} == {"present"}


def test_cap_flags_update_only_the_limits_of_an_existing_profile(env):
    options, _ = env
    _, accounts, _ = do_apply(options)
    path = real(options, options.profile_path)
    before = json.loads(path.read_text())
    lines_before = path.read_text().splitlines()
    changed = replace(
        options, max_cost_cents_per_call=20, daily_token_cap=None, profile_template=None
    )
    steps = {s.name: s for s in ri.plan(changed, accounts=accounts)}
    assert steps["profile"].state == "differs"
    assert steps["profile"].detail["caps"] == {"max_cost_cents_per_call": {"from": 10, "to": 20}}
    do_apply(changed, accounts=accounts)
    after = json.loads(path.read_text())
    assert after["limits"]["max_cost_cents_per_call"] == 20
    before["limits"]["max_cost_cents_per_call"] = 20
    assert after == before
    diff = [a for a, b in zip(lines_before, path.read_text().splitlines(), strict=True) if a != b]
    assert len(diff) == 1 and "max_cost_cents_per_call" in diff[0]
    assert stat.S_IMODE(path.lstat().st_mode) == 0o600
    # absent options leave the profile untouched
    untouched = path.read_bytes()
    do_apply(replace(changed, max_cost_cents_per_call=None), accounts=accounts)
    assert path.read_bytes() == untouched


@pytest.mark.parametrize(
    "caps",
    [
        {"max_cost_cents_per_call": 5001},
        {"daily_cost_cap_cents": 100001},
        {"max_cost_cents_per_call": 0},
        {"daily_token_cap": -1},
    ],
)
def test_the_real_validator_rejects_out_of_bound_caps(env, caps):
    options, _ = env
    _, accounts, _ = do_apply(options)
    path = real(options, options.profile_path)
    before = path.read_bytes()
    with pytest.raises(InstallRefused, match="validation"):
        ri.apply(
            replace(options, **caps),
            accounts=accounts,
            runner=FakeRunner(),
            readiness=readiness_fake(),
        )
    assert path.read_bytes() == before


def test_a_new_profile_with_out_of_bound_caps_is_refused_and_not_written(env):
    options, _ = env
    with pytest.raises(InstallRefused, match="validation"):
        ri.plan(replace(options, daily_cost_cap_cents=10**9), accounts=FakeAccounts())
    assert snapshot(options.root) == {}


def test_missing_template_and_missing_prompt_name_their_flags(env):
    options, _ = env
    with pytest.raises(InstallRefused, match="--profile-template"):
        ri.plan(replace(options, profile_template=None), accounts=FakeAccounts())
    with pytest.raises(InstallRefused, match="--prompt-source"):
        ri.plan(replace(options, prompt_source=None), accounts=FakeAccounts())
    # template without limits caps and no cap flags
    with pytest.raises(InstallRefused, match="--daily-token-cap"):
        ri.plan(replace(options, daily_token_cap=None), accounts=FakeAccounts())
    assert snapshot(options.root) == {}


def test_prompt_is_kept_when_no_source_and_size_is_bounded(env, tmp_path):
    options, src = env
    _, accounts, _ = do_apply(options)
    prompt = real(options, options.prompt_path)
    before = prompt.read_bytes()
    install_interpreter(options)
    kept = replace(options, prompt_source=None)
    assert {s.state for s in ri.plan(kept, accounts=accounts)} == {"present"}
    assert prompt.read_bytes() == before
    big = src / "big.txt"
    big.write_bytes(b"x" * 8193)
    with pytest.raises(InstallRefused, match="larger"):
        ri.plan(replace(options, prompt_source=big), accounts=accounts)


@pytest.mark.parametrize(
    "step,attr",
    [
        ("systemd_unit", "unit_path"),
        ("model_credential_reference", "reference_path"),
        ("prompt", "prompt_path"),
    ],
)
def test_a_differing_managed_file_is_refused_and_unchanged(env, step, attr):
    options, _ = env
    _, accounts, _ = do_apply(options)
    path = real(options, getattr(options, attr))
    path.write_text("operator edit\n")
    before = path.read_bytes()
    state = {s.name: s.state for s in ri.plan(options, accounts=accounts)}
    assert state[step] == "differs"
    runner = FakeRunner()
    with pytest.raises(InstallRefused, match="nothing was changed"):
        ri.apply(options, accounts=accounts, runner=runner, readiness=readiness_fake())
    assert path.read_bytes() == before and runner.calls == []
    # replace is the explicit way to overwrite
    ri.apply(
        replace(options, replace=True), accounts=accounts, runner=runner, readiness=readiness_fake()
    )
    assert path.read_bytes() != before


def test_symlinks_and_wrong_kinds_at_managed_paths_are_refused(env, tmp_path):
    options, _ = env
    do_apply(options)
    accounts = FakeAccounts(existing=True)
    unit = real(options, options.unit_path)
    unit.unlink()
    unit.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(InstallRefused, match="symlink"):
        ri.plan(options, accounts=accounts)
    unit.unlink()
    state = real(options, options.state_dir)
    state.rmdir()
    state.symlink_to(tmp_path)
    with pytest.raises(InstallRefused, match="symlink"):
        ri.plan(options, accounts=accounts)
    state.unlink()
    state.write_text("x")
    with pytest.raises(InstallRefused, match="not a directory"):
        ri.plan(options, accounts=accounts)


def test_a_writable_parent_is_refused(env):
    options, _ = env
    parent = real(options, "/etc")
    parent.mkdir()
    parent.chmod(0o777)
    with pytest.raises(InstallRefused, match="writable"):
        ri.plan(options, accounts=FakeAccounts())


def test_apply_needs_root_without_the_root_option(env, monkeypatch):
    options, _ = env
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    with pytest.raises(InstallRefused, match="apply needs root"):
        ri.apply(replace(options, root=None), accounts=FakeAccounts(), runner=FakeRunner())


def test_key_and_token_files_are_never_created_opened_or_modified(env, monkeypatch):
    options, _ = env
    creds = real(options, options.credentials_dir)
    creds.mkdir(parents=True)
    creds.chmod(0o700)
    guarded = [creds / "reasoning-anthropic-api-key", creds / "reasoning-agent-token"]
    for path in guarded:
        path.write_text("secret-value\n")
        path.chmod(0)
    marks = [(p.lstat().st_mtime_ns, p.lstat().st_ino) for p in guarded]
    opened = []
    real_open = os.open
    monkeypatch.setattr(
        os, "open", lambda p, *a, **k: (opened.append(str(p)), real_open(p, *a, **k))[1]
    )
    do_apply(options)
    do_apply(options)
    assert not [p for p in opened if p.endswith(("api-key", "agent-token"))]
    assert [(p.lstat().st_mtime_ns, p.lstat().st_ino) for p in guarded] == marks
    # and nothing else appeared beside them except the reference
    assert {p.name for p in creds.iterdir()} == {
        "reasoning-anthropic-api-key",
        "reasoning-agent-token",
        "reasoning-model-credential",
    }
    assert not (real(options, options.config_dir) / "reasoning.admitted").exists()


def test_render_unit_default_equals_the_shipped_unit_byte_for_byte():
    shipped = (REPO / "deploy/remote-agent-platform/tessera-reasoning.service").read_text()
    assert ri.render_unit(Options()) == shipped


def test_unit_has_no_install_and_nothing_is_enabled_or_started(env):
    options, _ = env
    _, _, runner = do_apply(options)
    unit = real(options, options.unit_path).read_text()
    lines = [line.strip() for line in unit.splitlines() if not line.startswith("#")]
    assert "[Install]" not in lines and not any(x.startswith("WantedBy") for x in lines)
    flat = [" ".join(call) for call in runner.calls]
    assert flat == ["systemctl daemon-reload"]
    assert not any(w in flat[0] for w in ("enable", "start"))


def test_render_unit_substitutes_custom_values(env):
    options, _ = env
    custom = replace(
        options,
        user="svc-r",
        python="/usr/bin/py",
        state_dir=Path("/srv/state"),
        credentials_dir=Path("/srv/cred"),
    )
    text = ri.render_unit(custom)
    assert "User=svc-r\nGroup=svc-r" in text
    assert (
        "ExecStart=/usr/bin/py -m tessera_agent.service_startup reasoning --state-dir /srv/state"
        in text
    )
    assert "LoadCredential=model-credential:/srv/cred/reasoning-model-credential" in text
    assert "StateDirectory=" not in text.replace("StateDirectoryMode", "")
    with pytest.raises(InstallRefused):
        ri.render_unit(replace(options, user="bad user"))


def test_the_written_profile_passes_the_real_validator_with_the_prompt_digest(env):
    from tessera_agent.remote_reasoning_runtime import validate_profile

    options, _ = env
    do_apply(options)
    profile = json.loads(real(options, options.profile_path).read_text())
    validate_profile(profile)
    import hashlib

    assert profile["prompt_sha256"] == hashlib.sha256(b"synthetic prompt\n").hexdigest()
    assert profile["limits"]["daily_token_cap"] == 10000


def test_no_profile_value_reaches_plan_output_or_refusals(env):
    options, _ = env
    _, accounts, _ = do_apply(options)
    texts = [json.dumps([vars(s) for s in ri.plan(options, accounts=accounts)], default=str)]
    # a refusal raised from validating a profile that carries the sentinels
    template = json.loads(options.profile_template.read_text())
    template["limits"]["max_output_tokens_per_call"] = "bad"
    bad = options.profile_template.parent / "bad.json"
    bad.write_text(json.dumps(template))
    for override in (
        {"profile_template": bad, "profile_path": Path("/etc/tessera/other.json")},
        {
            "profile_template": bad,
            "profile_path": Path("/etc/tessera/other.json"),
            "root": options.root,
        },
    ):
        with pytest.raises(InstallRefused) as error:
            ri.plan(replace(options, **override), accounts=accounts)
        texts.append(str(error.value))
    real(options, options.unit_path).write_text("other\n")
    with pytest.raises(InstallRefused) as error:
        ri.apply(options, accounts=accounts, runner=FakeRunner(), readiness=readiness_fake())
    texts.append(str(error.value))
    result, _, _ = do_apply(replace(options, replace=True), accounts=accounts)
    texts.append(json.dumps(result, default=str))
    for text in texts:
        assert (
            SENTINEL_MODEL not in text and SENTINEL_REF not in text and "sentineowner" not in text
        )


def test_a_failing_step_stops_apply_and_lists_what_changed(env):
    options, _ = env

    class FailingRunner(FakeRunner):
        def __call__(self, argv):
            super().__call__(argv)
            return 1

    result = ri.apply(
        options,
        accounts=FakeAccounts(),
        runner=FailingRunner(),
        readiness=readiness_fake(),
    )
    assert result["exit_code"] == 1 and result["readiness"] is None
    assert [s["name"] for s in result["steps"]][-1] == "systemd_unit"
    assert result["steps"][-1]["result"] == "failed"
    assert [s["result"] for s in result["steps"][:-1]] == ["created"] * 7 + ["skipped"]


def test_the_readiness_check_is_told_which_owners_the_install_made(env):
    options, _src = env
    ready = readiness_fake()
    _result, accounts, _runner = do_apply(options, ready=ready)
    seen = ready.calls[-1]
    assert seen["key_owner_uid"] == 0  # the key file is root-owned (ADR 0093)
    assert seen["state_owner_uid"] == accounts.lookup(options.user)[0]


# -- the real readiness check, not a stand-in (review finding 1) -----------------------------------


class SameUserAccounts(FakeAccounts):
    """A service account that is the test user, so real ownership checks can hold unprivileged."""

    def lookup(self, user):
        return (os.getuid(), os.getgid())

    def create(self, user):
        self.created.append(user)


def _control_state(tmp_path, project="proj1"):
    import sqlite3

    from tessera_control.model_spend import create_model_spend_schema

    control = tmp_path / "control"
    control.mkdir()
    database = sqlite3.connect(control / "tessera.sqlite3")
    create_model_spend_schema(database)
    database.execute(
        "INSERT INTO model_spend_limits VALUES ('org1', ?, 'anthropic', 20000000, 90000000, 1, "
        "'p', 'now')",
        (project,),
    )
    database.commit()
    database.close()
    return control


def _real_readiness(monkeypatch):
    from functools import partial

    from tessera_agent import first_call_readiness as fcr

    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    opener = lambda method, url, timeout: 200 if url.endswith("/ready") else 401  # noqa: E731
    return partial(fcr.run_checks, opener=opener, environment={"PATH": "/usr/bin"})


def test_after_an_install_the_real_check_finds_the_files_it_made_and_only_the_key_is_missing(
    env, tmp_path, monkeypatch
):
    options, _ = env
    options = replace(
        options,
        control_state_dir=_control_state(tmp_path),
        control_url="http://control.invalid",
        project_id="proj1",
    )
    result = ri.apply(
        options,
        accounts=SameUserAccounts(),
        runner=FakeRunner(),
        readiness=_real_readiness(monkeypatch),
    )
    checks = {c["name"]: c["status"] for c in result["readiness"]["checks"]}
    # The files are stored as reasoning-model-credential and reasoning-anthropic-api-key; the
    # check is told the names the service will see them under.
    assert checks["credential_reference"] == "ok"
    assert checks["api_key_file"] == "missing"
    assert not [name for name, state in checks.items() if state == "fail"], checks
    assert result["readiness"]["overall"] == "ready_except_key"
    assert result["exit_code"] == 10


def test_the_documented_next_step_works_after_install_and_the_check_then_sees_the_key(
    env, tmp_path, monkeypatch
):
    import io

    from tessera_agent import service_credentials as sc

    options, _ = env
    options = replace(
        options,
        control_state_dir=_control_state(tmp_path),
        control_url="http://control.invalid",
        project_id="proj1",
    )
    accounts, runner = SameUserAccounts(), FakeRunner()
    ri.apply(options, accounts=accounts, runner=runner, readiness=_real_readiness(monkeypatch))
    credentials = real(options, options.credentials_dir)
    reference = real(options, options.reference_path)
    before = (reference.read_bytes(), reference.stat().st_ino)
    receipt = sc.install(
        "tessera-reasoning",
        "anthropic-api-key",
        io.BytesIO(b"synthetic-key-0123456789\n"),
        credentials_dir=credentials,
        unit_dirs=[],
        environ={},
        owner_label="sentineowner",
        reference_label="sentinelref",
        require_root=False,
        receipts_path=tmp_path / "receipts.jsonl",
        profile_path=real(options, options.profile_path),
    )
    assert receipt["replaced"] is False  # only the key was written; no --replace
    assert (reference.read_bytes(), reference.stat().st_ino) == before  # the reference was kept
    key = credentials / ri.KEY_FILE
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    from functools import partial

    check = partial(_real_readiness(monkeypatch), key_owner_uid=os.getuid())
    report = check(
        profile_path=real(options, options.profile_path),
        credentials_directory=credentials,
        state_dir=real(options, options.state_dir),
        control_state_dir=options.control_state_dir,
        control_url=options.control_url,
        project_id=options.project_id,
        credential_files=ri.credential_files(options),
    )
    states = {c["name"]: c["status"] for c in report["checks"]}
    assert states["api_key_file"] == "ok" and states["credential_reference"] == "ok"
    assert report["overall"] == "ready"


def test_a_different_reference_file_still_needs_replace(env, tmp_path):
    import io

    from tessera_agent import service_credentials as sc

    options, _ = env
    ri.apply(options, accounts=SameUserAccounts(), runner=FakeRunner(), readiness=readiness_fake())
    reference = real(options, options.reference_path)
    reference.write_text("anthropic-api-key://other/other\n")
    reference.chmod(0o600)
    with pytest.raises(sc.CredentialRefused):
        sc.install(
            "tessera-reasoning",
            "anthropic-api-key",
            io.BytesIO(b"synthetic-key-0123456789\n"),
            credentials_dir=real(options, options.credentials_dir),
            unit_dirs=[],
            environ={},
            owner_label="sentineowner",
            reference_label="sentinelref",
            require_root=False,
            receipts_path=tmp_path / "receipts.jsonl",
        )


# -- the interpreter (review finding 3) -------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [f"/{top}/venv/bin/python" for top in ("home", "root", "run", "tmp")],
)
def test_an_interpreter_the_unit_cannot_reach_is_refused(env, path):
    options, _ = env
    with pytest.raises(InstallRefused, match="cannot reach"):
        ri.plan(replace(options, python=path), accounts=FakeAccounts())


def test_a_missing_interpreter_is_shown_and_never_created(env):
    options, _ = env
    steps = {s.name: s for s in ri.plan(options, accounts=FakeAccounts())}
    assert steps["interpreter"].state == "missing"
    do_apply(options)
    assert not real(options, options.python).exists()  # installing Tessera is the operator's step
    install_interpreter(options)
    assert {s.name: s for s in ri.plan(options, accounts=SameUserAccounts())}[
        "interpreter"
    ].state == "present"
