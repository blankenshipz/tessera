from __future__ import annotations

import io
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest
from test_first_call_readiness import world  # noqa: F401 - fixture
from test_reasoning_credential_references import anthropic  # noqa: F401 - fixture

from tessera_agent import first_call_readiness as fcr
from tessera_agent import readiness_in_unit as riu

UNIT = """[Unit]
Description=x
[Service]
User=svc
Group=svcg
StateDirectory=tessera-reasoning
WorkingDirectory=/var/lib/other
LoadCredential=profile:/etc/t/p.json
LoadCredential=agent-token:/etc/t/c/tok
ExecStart=/opt/py/bin/python -m tessera_agent.service_startup reasoning
"""
REPO_UNIT = (
    Path(__file__).resolve().parents[1] / "deploy/remote-agent-platform/tessera-reasoning.service"
)


REPORT = '{"overall": "ready_except_key"}'


def fake_lstat(missing=(), modes=None):
    """An lstat where every path is a regular file except the missing ones and given modes."""
    modes = modes or {}

    def lstat(path):
        if path in missing:
            raise FileNotFoundError(path)
        return os.stat_result((modes.get(path, stat.S_IFREG | 0o600),) + (0,) * 9)

    return lstat


ALL_PRESENT = fake_lstat()


def _unit(tmp_path, text=UNIT):
    p = tmp_path / "u.service"
    p.write_text(text)
    return p


def test_parse_properties():
    p = riu.unit_properties(UNIT)
    assert p["user"] == "svc" and p["group"] == "svcg"
    assert p["load_credentials"] == ["profile:/etc/t/p.json", "agent-token:/etc/t/c/tok"]
    assert p["python"] == "/opt/py/bin/python"
    assert p["state_dir"] == "/var/lib/tessera-reasoning"


def test_working_directory_fallback():
    p = riu.unit_properties("[Service]\nUser=a\nLoadCredential=x:/y\nWorkingDirectory=/srv/w\n")
    assert p["state_dir"] == "/srv/w" and p["group"] is None and p["python"] is None


@pytest.mark.parametrize("text", ["[Service]\nLoadCredential=x:/y\n", "[Service]\nUser=a\n"])
def test_refusals(text):
    with pytest.raises(ValueError) as e:
        riu.unit_properties(text)
    assert "/y" not in str(e.value)


def test_transient_argv_exact():
    argv = riu.transient_argv(
        riu.unit_properties(UNIT),
        state_dir="/s",
        control_state_dir="/c",
        control_url="http://h",
        project_id="p",
        lstat=ALL_PRESENT,
    )
    assert argv == [
        "systemd-run", "--pipe", "--wait", "--collect", "--quiet",
        "-p", "User=svc", "-p", "Group=svcg",
        "-p", "WorkingDirectory=/var/lib/other", "-p", "StateDirectory=tessera-reasoning",
        "-p", "LoadCredential=profile:/etc/t/p.json",
        "-p", "LoadCredential=agent-token:/etc/t/c/tok",
        "--", "/opt/py/bin/python", "-m", "tessera_agent.first_call_readiness", "check",
        "--state-dir", "/s", "--control-state-dir", "/c", "--control-url", "http://h",
        "--project-id", "p",
    ]  # fmt: skip
    assert "sh" not in argv and "-c" not in argv


def test_run_passthrough_and_state_default(tmp_path):
    seen = []

    def runner(argv):
        seen.append(argv)
        return 10, REPORT

    assert riu.run_in_unit(_unit(tmp_path), runner=runner, lstat=ALL_PRESENT) == (10, REPORT)
    assert seen[0][seen[0].index("--state-dir") + 1] == "/var/lib/tessera-reasoning"


@pytest.mark.parametrize("code", [0, 10, 11, 1])
def test_documented_codes_pass_the_report_through(tmp_path, code):
    result = riu.run_in_unit(_unit(tmp_path), runner=lambda a: (code, REPORT), lstat=ALL_PRESENT)
    assert result == (code, REPORT)


@pytest.mark.parametrize("code", [243, 127, -9, 2, 3, 78, 255, None])
def test_undocumented_codes_did_not_complete(tmp_path, code):
    leak = "systemd: Failed to set up credentials: /etc/t/c/tok SECRET-ISH"
    result = riu.run_in_unit(_unit(tmp_path), runner=lambda a: (code, leak), lstat=ALL_PRESENT)
    assert result == (1, riu.DID_NOT_COMPLETE)


@pytest.mark.parametrize("output", ["", "not json", "[1]", '{"overall": ', None])
def test_documented_code_without_a_json_report_did_not_complete(tmp_path, output):
    result = riu.run_in_unit(_unit(tmp_path), runner=lambda a: (0, output), lstat=ALL_PRESENT)
    assert result == (1, riu.DID_NOT_COMPLETE)


def test_runner_exception_did_not_complete(tmp_path):
    def runner(argv):
        raise OSError("systemd-run: /secret/path not found")

    assert riu.run_in_unit(_unit(tmp_path), runner=runner, lstat=ALL_PRESENT) == (
        1,
        riu.DID_NOT_COMPLETE,
    )


def _loaded(argv):
    return [
        argv[i + 1].removeprefix("LoadCredential=")
        for i, word in enumerate(argv[:-1])
        if word == "-p" and argv[i + 1].startswith("LoadCredential=")
    ]


SHIPPED = riu.unit_properties(REPO_UNIT.read_text())
KEY_SOURCE = "/etc/tessera/credentials/reasoning-anthropic-api-key"
TOKEN_SOURCE = "/etc/tessera/credentials/reasoning-agent-token"


def test_missing_key_file_is_left_out_and_the_rest_kept():
    argv = riu.transient_argv(SHIPPED, state_dir="/s", lstat=fake_lstat(missing={KEY_SOURCE}))
    assert not any(line.startswith("anthropic-api-key:") for line in _loaded(argv))
    assert _loaded(argv) == [
        line for line in SHIPPED["load_credentials"] if not line.startswith("anthropic-api-key:")
    ]


def test_missing_agent_token_is_left_out_too():
    argv = riu.transient_argv(
        SHIPPED, state_dir="/s", lstat=fake_lstat(missing={KEY_SOURCE, TOKEN_SOURCE})
    )
    names = [line.partition(":")[0] for line in _loaded(argv)]
    assert names == ["profile", "prompt", "model-credential"]


@pytest.mark.parametrize(
    "mode", [stat.S_IFLNK | 0o777, stat.S_IFDIR | 0o700, stat.S_IFIFO | 0o600, stat.S_IFSOCK]
)
def test_symlink_or_non_regular_source_is_left_out(mode):
    argv = riu.transient_argv(SHIPPED, state_dir="/s", lstat=fake_lstat(modes={KEY_SOURCE: mode}))
    assert [line.partition(":")[0] for line in _loaded(argv)] == SHIPPED["credential_names"][:-1]


@pytest.mark.parametrize("error", [PermissionError, NotADirectoryError, OSError])
def test_unstatable_source_is_left_out(error):
    def lstat(path):
        if path == TOKEN_SOURCE:
            raise error(path)
        return ALL_PRESENT(path)

    argv = riu.transient_argv(SHIPPED, state_dir="/s", lstat=lstat)
    assert "agent-token" not in [line.partition(":")[0] for line in _loaded(argv)]
    assert len(_loaded(argv)) == 4


def test_every_present_credential_passes_unchanged_and_store_names_untouched():
    props = riu.unit_properties(UNIT + "LoadCredential=stored:from-credstore\n")
    seen = []

    def lstat(path):
        seen.append(path)
        return ALL_PRESENT(path)

    argv = riu.transient_argv(props, state_dir="/s", lstat=lstat)
    assert _loaded(argv) == [
        "profile:/etc/t/p.json",
        "agent-token:/etc/t/c/tok",
        "stored:from-credstore",
    ]
    assert seen == ["/etc/t/p.json", "/etc/t/c/tok"]
    assert (
        _loaded(riu.transient_argv(SHIPPED, state_dir="/s", lstat=ALL_PRESENT))
        == (SHIPPED["load_credentials"])
    )


def test_run_in_unit_without_the_key_reports_ready_except_key(tmp_path):
    """A fake systemd: credentials it is not given are missing; a missing source fails it (243)."""
    unit = UNIT.replace(
        "LoadCredential=agent-token",
        "LoadCredential=anthropic-api-key:/k/key\nLoadCredential=agent-token",
    )

    def runner(argv):
        loaded = _loaded(argv)
        if any(line.partition(":")[2] == "/k/key" for line in loaded):
            return 243, ""  # what systemd does with a missing LoadCredential source
        if [line.partition(":")[0] for line in loaded] == ["profile", "agent-token"]:
            return 10, REPORT
        return 1, '{"overall": "not_ready"}'

    code, output = riu.run_in_unit(
        _unit(tmp_path, unit), runner=runner, lstat=fake_lstat(missing={"/k/key"})
    )
    assert (code, output) == (10, REPORT)


def test_real_check_without_the_key_exits_10(world, monkeypatch, capsys):  # noqa: F811
    """The real check, given only the credentials the transient unit would load, exits 10."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(fcr, "_default_opener", world["args"]["opener"])
    root = world["root"]
    sources = {name: f"/etc/x/{name}" for name in SHIPPED["credential_names"]}
    unit = "[Service]\nUser=svc\nExecStart=/py/bin/python -m x\n" + "".join(
        f"LoadCredential={name}:{source}\n" for name, source in sources.items()
    )
    unit_path = world["state"].parent / "real.service"
    unit_path.write_text(unit)
    (root / "anthropic-api-key").unlink()
    missing = {sources[name] for name in sources if not (root / name).exists()}

    def runner(argv):
        # Stand-in for systemd-run: lay out exactly the loaded credentials, then run the check.
        for name in sources:
            if name not in [line.partition(":")[0] for line in _loaded(argv)]:
                assert not (root / name).exists()
        tail = argv[argv.index("--") + 5 :]
        monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(root))
        code = fcr.main(["check", *tail])
        return code, capsys.readouterr().out

    code, output = riu.run_in_unit(
        unit_path,
        state_dir=world["state"],
        control_state_dir=world["control"],
        control_url="http://control.invalid",
        runner=runner,
        lstat=fake_lstat(missing=missing),
    )
    assert code == 10 and json.loads(output)["overall"] == "ready_except_key"


SANDBOXED = """[Service]
User=svc
UMask=0077
StateDirectory=tessera-reasoning
StateDirectoryMode=0700
WorkingDirectory=/var/lib/tessera-reasoning
Environment=A=1
Environment="B=two words" C=3
LoadCredential=profile:/etc/t/p.json
ExecStart=/py -m x
NoNewPrivileges=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=/var/lib/tessera-reasoning
CapabilityBoundingSet=
Restart=on-failure
[Install]
Environment=NOT_SERVICE=1
"""


def _properties(argv):
    return [argv[i + 1] for i, word in enumerate(argv[: argv.index("--")]) if word == "-p"]


def test_sandbox_properties_are_copied_and_nothing_else():
    argv = riu.transient_argv(riu.unit_properties(SANDBOXED), state_dir="/s", lstat=ALL_PRESENT)
    assert _properties(argv) == [
        "User=svc",
        "UMask=0077",
        "WorkingDirectory=/var/lib/tessera-reasoning",
        "StateDirectory=tessera-reasoning",
        "StateDirectoryMode=0700",
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "PrivateTmp=yes",
        "NoNewPrivileges=yes",
        "ReadWritePaths=/var/lib/tessera-reasoning",
        "LoadCredential=profile:/etc/t/p.json",
    ]
    for absent in ("PrivateDevices", "CapabilityBoundingSet", "Restart", "Environment"):
        assert not any(p.startswith(absent + "=") for p in _properties(argv))
    assert "NOT_SERVICE" not in " ".join(argv)


@pytest.mark.parametrize("name", riu.SANDBOX_PROPERTIES)
def test_each_sandbox_property_alone(name):
    text = f"[Service]\nUser=u\nExecStart=/py\nLoadCredential=p:/p\n{name}=v-{name}\n"
    argv = riu.transient_argv(riu.unit_properties(text), state_dir="/s", lstat=ALL_PRESENT)
    assert _properties(argv) == ["User=u", f"{name}=v-{name}", "LoadCredential=p:/p"]


def test_shipped_unit_sandbox_is_copied():
    argv = riu.transient_argv(SHIPPED, state_dir="/s", lstat=ALL_PRESENT)
    props = _properties(argv)
    for expected in (
        "UMask=0077",
        "StateDirectory=tessera-reasoning",
        "StateDirectoryMode=0700",
        "WorkingDirectory=/var/lib/tessera-reasoning",
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "PrivateTmp=yes",
        "NoNewPrivileges=yes",
    ):
        assert expected in props


class FakeProcess:
    def __init__(self, data, code=0):
        self.stdout = io.BytesIO(data)
        self.code = code
        self.killed = False
        self.read_sizes = []
        read = self.stdout.read

        def tracked(size=-1):
            self.read_sizes.append(size)
            return read(size)

        self.stdout.read = tracked

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def kill(self):
        self.killed = True

    def wait(self):
        return -9 if self.killed else self.code


def _fake_popen(monkeypatch, process, captured):
    def popen(argv, **kw):
        captured.update(kw, argv=argv)
        return process

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(os, "geteuid", lambda: 0)


def test_default_runner_path_only(monkeypatch, tmp_path):
    captured = {}
    _fake_popen(monkeypatch, FakeProcess(REPORT.encode(), code=10), captured)
    monkeypatch.setenv("PATH", "/bin")
    monkeypatch.setenv("SECRET_X", "v")
    assert riu.run_in_unit(_unit(tmp_path)) == (10, REPORT)
    assert captured["env"] == {"PATH": "/bin"} and not captured.get("shell")
    assert captured["argv"][0] == "systemd-run"


def test_default_runner_oversized_output_did_not_complete(monkeypatch, tmp_path):
    captured = {}
    big = ('{"overall": "ready", "pad": "' + "a" * riu.MAX_OUTPUT_BYTES + '"}').encode()
    process = FakeProcess(big, code=0)
    _fake_popen(monkeypatch, process, captured)
    assert riu.run_in_unit(_unit(tmp_path)) == (1, riu.DID_NOT_COMPLETE)
    assert process.read_sizes == [riu.MAX_OUTPUT_BYTES + 1] and process.killed


def test_default_runner_output_at_the_limit_passes(monkeypatch, tmp_path):
    body = '{"overall": "ready", "pad": "'
    data = (body + "a" * (riu.MAX_OUTPUT_BYTES - len(body) - 2) + '"}').encode()
    assert len(data) == riu.MAX_OUTPUT_BYTES
    process = FakeProcess(data, code=0)
    _fake_popen(monkeypatch, process, {})
    assert riu.run_in_unit(_unit(tmp_path)) == (0, data.decode())
    assert not process.killed


def test_non_root_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    with pytest.raises(ValueError):
        riu.run_in_unit(_unit(tmp_path))


def test_symlink_refused(tmp_path):
    link = tmp_path / "l.service"
    link.symlink_to(_unit(tmp_path))
    with pytest.raises(ValueError):
        riu.run_in_unit(link, runner=lambda a: (0, ""))


def test_shipped_unit():
    p = riu.unit_properties(REPO_UNIT.read_text())
    assert p["credential_names"] == [
        "profile",
        "prompt",
        "agent-token",
        "model-credential",
        "anthropic-api-key",
    ]
    assert p["user"] == "tessera-reasoning" and p["state_dir"] == "/var/lib/tessera-reasoning"


def test_main_in_unit(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(riu, "run_in_unit", lambda *a, **k: (4, '{"ok": 1}'))
    assert fcr.main(["check", "--in-unit", "--unit-path", str(tmp_path / "x")]) == 4
    assert json.loads(capsys.readouterr().out) == {"ok": 1}


def test_main_in_unit_refusal(monkeypatch, tmp_path):
    assert fcr.main(["check", "--in-unit", "--unit-path", str(tmp_path / "missing")]) == 2


def test_main_defaults_from_environment(monkeypatch, tmp_path):
    seen = {}

    def fake(**kw):
        seen.update(kw)
        return {"overall": "ready", "summary": "s"}

    monkeypatch.setattr(fcr, "run_checks", fake)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    fcr.main(["check", "--state-dir", str(tmp_path / "st")])
    assert (
        seen["credentials_directory"] == tmp_path and seen["profile_path"] == tmp_path / "profile"
    )


@pytest.mark.parametrize("value", [None, "relative/dir"])
def test_main_refuses_without_flag_or_env(monkeypatch, tmp_path, value):
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    if value:
        monkeypatch.setenv("CREDENTIALS_DIRECTORY", value)
    with pytest.raises(SystemExit) as stop:
        fcr.main(["check", "--state-dir", str(tmp_path)])
    assert stop.value.code == 2


SRV_UNIT = """[Service]
User=svc
Group=svc
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=/srv/tessera-reasoning
ReadWritePaths=/srv/tessera-extra /srv/tessera-more
LoadCredential=profile:/etc/t/p.json
ExecStart=/py -m x
"""


def test_a_state_directory_outside_var_lib_stays_writable_in_the_check():
    # The installer writes ProtectSystem=strict plus ReadWritePaths= (no StateDirectory=) for such a
    # directory; the transient unit must carry ReadWritePaths= or the state directory is read-only.
    argv = riu.transient_argv(riu.unit_properties(SRV_UNIT), state_dir="/srv/x", lstat=ALL_PRESENT)
    props = _properties(argv)
    assert "ProtectSystem=strict" in props
    assert "ReadWritePaths=/srv/tessera-reasoning" in props
    assert "ReadWritePaths=/srv/tessera-extra /srv/tessera-more" in props


SECRETISH = """[Service]
User=svc
LoadCredential=profile:/etc/t/p.json
Environment=SOME_TOKEN=hunter2-not-a-real-secret
Environment="OTHER=two words" PLAIN=value
ExecStart=/py -m x
"""


def test_no_environment_value_reaches_the_command_line():
    props = riu.unit_properties(SECRETISH)
    assert props["environment_names"] == ["SOME_TOKEN", "OTHER", "PLAIN"]
    argv = riu.transient_argv(props, state_dir="/s", lstat=ALL_PRESENT)
    text = " ".join(argv)
    for leaked in ("hunter2", "two words", "PLAIN=value", "Environment"):
        assert leaked not in text


@pytest.mark.parametrize("name", sorted(riu.FORBIDDEN_ENVIRONMENT))
def test_the_in_unit_run_refuses_a_unit_that_sets_a_key_variable(tmp_path, name):
    unit = tmp_path / "u.service"
    unit.write_text(SECRETISH.replace("SOME_TOKEN", name))
    calls = []
    with pytest.raises(ValueError, match="credential environment variable") as caught:
        riu.run_in_unit(
            unit,
            state_dir="/s",
            runner=lambda argv: calls.append(argv) or (0, "{}"),
            lstat=ALL_PRESENT,
        )
    assert calls == []  # nothing was started
    assert "hunter2" not in str(caught.value)
