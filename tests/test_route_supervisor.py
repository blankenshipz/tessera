import asyncio
import json
import os
import plistlib
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from tessera_operator import route_supervisor as supervisor


@pytest.mark.parametrize("is_error", [False, True])
def test_only_guarded_operator_tool(monkeypatch, is_error):
    calls = []

    @asynccontextmanager
    async def stdio(params, errlog):
        assert params.args == ["-m", "tessera_operator.kubernetes"]
        assert params.command.endswith("/.venv/bin/python")
        assert "TOKEN" not in str(params.env)
        yield "read", "write"

    class Session:
        def __init__(self, read, write):
            assert (read, write) == ("read", "write")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def initialize(self):
            calls.append("initialize")

        async def call_tool(self, name, args):
            calls.append((name, args))
            return SimpleNamespace(isError=is_error)

    monkeypatch.setattr(supervisor, "stdio_client", stdio)
    monkeypatch.setattr(supervisor, "ClientSession", Session)
    assert asyncio.run(supervisor.reconcile()) is not is_error
    assert calls == ["initialize", ("local_loopback_route", {})]


@pytest.mark.parametrize("outcome", [True, False, RuntimeError("sensitive diagnostics")])
def test_tick_sanitizes_failure_and_releases_lock(tmp_path, monkeypatch, outcome, capsys):
    tmp_path.chmod(0o700)
    monkeypatch.setattr(supervisor, "PRIVATE", tmp_path)
    monkeypatch.setattr(supervisor.local_paths, "REPOSITORY_CONFIGURED", True)

    async def tick():
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(supervisor, "bounded_tick", tick)
    assert supervisor.main() == (0 if outcome is True else 1)
    err = capsys.readouterr().err
    if outcome is True:
        assert err == ""
    else:
        assert err == "route-supervisor: route not verified; inspect operator receipts\n"
        assert "sensitive" not in err
    path = tmp_path / "local-route-supervisor-status.json"
    assert path.stat().st_mode & 0o777 == 0o600
    record = json.loads(path.read_text())
    assert set(record) == {"checked_at", "state"}
    assert record["state"] == (
        "route_verified" if outcome is True else "blocked_inspect_operator_receipts"
    )
    fd = supervisor.private_lock(tmp_path)
    os.close(fd)


def test_main_refuses_without_configured_repository(tmp_path, monkeypatch, capsys):
    tmp_path.chmod(0o700)
    monkeypatch.setattr(supervisor, "PRIVATE", tmp_path)
    monkeypatch.setattr(supervisor.local_paths, "REPOSITORY_CONFIGURED", False)

    async def tick():
        raise AssertionError("must not reconcile without a fixed target")

    monkeypatch.setattr(supervisor, "bounded_tick", tick)
    assert supervisor.main() == 1
    assert capsys.readouterr().err == "route-supervisor: repository is not configured\n"
    assert not (tmp_path / "local-route-supervisor-status.json").exists()
    assert not (tmp_path / "local-route-supervisor.lock").exists()


def test_main_says_why_when_lock_is_unavailable(tmp_path, monkeypatch, capsys):
    tmp_path.chmod(0o700)
    monkeypatch.setattr(supervisor, "PRIVATE", tmp_path)
    monkeypatch.setattr(supervisor.local_paths, "REPOSITORY_CONFIGURED", True)

    async def tick():
        raise AssertionError("must not reconcile without the lock")

    monkeypatch.setattr(supervisor, "bounded_tick", tick)
    held = supervisor.private_lock(tmp_path)
    try:
        assert supervisor.main() == 1
        assert capsys.readouterr().err == (
            "route-supervisor: another supervisor tick holds the lock\n"
        )
    finally:
        os.close(held)
    tmp_path.chmod(0o755)
    assert supervisor.main() == 1
    assert capsys.readouterr().err == (
        "route-supervisor: private supervisor directory or lock is not acceptable\n"
    )


def test_lock_refuses_overlap_and_symlink(tmp_path):
    tmp_path.chmod(0o700)
    fd = supervisor.private_lock(tmp_path)
    try:
        with pytest.raises(BlockingIOError):
            supervisor.private_lock(tmp_path)
    finally:
        os.close(fd)
    lock = tmp_path / "local-route-supervisor.lock"
    lock.unlink()
    lock.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(OSError):
        supervisor.private_lock(tmp_path)


def test_launch_agent_is_bounded_operator_not_raw_kubectl():
    path = Path("deploy/kubernetes/dev.tessera.local-route.plist")
    template = path.read_text()
    assert "__REPOSITORY__" in template
    job = plistlib.loads(template.replace("__REPOSITORY__", str(supervisor.ROOT)).encode())
    assert job["ProgramArguments"] == [
        str(supervisor.ROOT / ".venv/bin/python"),
        "-m",
        "tessera_operator.route_supervisor",
    ]
    assert job["StartInterval"] == 60
    assert job["RunAtLoad"] is True
    assert "KeepAlive" not in job


def test_private_directory_refuses_shared_mode(tmp_path):
    tmp_path.chmod(0o755)
    with pytest.raises(ValueError, match="private supervisor directory"):
        supervisor.private_lock(tmp_path)


def test_tick_has_bounded_timeout(monkeypatch):
    seen = []

    @asynccontextmanager
    async def timeout(seconds):
        seen.append(seconds)
        yield

    async def reconcile():
        return True

    monkeypatch.setattr(supervisor.asyncio, "timeout", timeout)
    monkeypatch.setattr(supervisor, "reconcile", reconcile)
    assert asyncio.run(supervisor.bounded_tick()) is True
    assert seen == [90]
