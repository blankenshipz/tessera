"""Start-up refuses a bad recorded admission (exit 78, code only); bare or absent passes."""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tessera_agent import readiness_in_unit as riu
from tessera_agent import reasoning_install, service_startup
from tessera_agent.reasoning_admission import marker_bytes

REPO = Path(__file__).resolve().parent.parent
UNIT = REPO / "deploy" / "remote-agent-platform" / "tessera-reasoning.service"
DIGEST = "a" * 64


def marker(expires: datetime) -> bytes:
    return marker_bytes(
        {
            "id": "adm-1",
            "record_digest": DIGEST,
            "project_id": "p1",
            "expires_at": expires.isoformat(),
        }
    )


def creds(tmp_path, content: bytes | None):
    root = tmp_path / "creds"
    root.mkdir(mode=0o700)
    if content is not None:
        (root / "admission").write_bytes(content)
        (root / "admission").chmod(0o600)
    return {"CREDENTIALS_DIRECTORY": str(root)}


def future():
    return datetime.now(UTC) + timedelta(days=2)


def test_absent_bare_and_valid_pass(tmp_path):
    assert service_startup.admission_refusal({}) is None
    for index, content in enumerate((None, b"", b"hand-made\n", marker(future()))):
        sub = tmp_path / str(index)
        sub.mkdir()
        assert service_startup.admission_refusal(creds(sub, content)) is None


@pytest.mark.parametrize(
    ("content", "code"),
    [
        pytest.param(
            marker(datetime.now(UTC) - timedelta(days=1)), "admission_expired", id="expired"
        ),
        pytest.param(b'{"kind": "x"}', "admission_marker_malformed", id="malformed"),
    ],
)
def test_main_exits_78_with_code_only(tmp_path, monkeypatch, capsys, content, code):
    environment = creds(tmp_path, content)
    monkeypatch.setattr(os, "environ", environment)
    monkeypatch.setattr(
        service_startup, "launch_spec", lambda *a, **k: pytest.fail("must not launch")
    )
    state = tmp_path / "state"
    assert service_startup.main(["reasoning", "--state-dir", str(state)]) == 78
    err = capsys.readouterr().err
    assert code in err and err.count("\n") == 1
    assert "adm-1" not in err and DIGEST not in err and "p1" not in err


def test_unit_and_template_reference_the_marker_and_never_restart_78():
    template = reasoning_install._UNIT_TEMPLATE
    for text in (UNIT.read_text(), template):
        assert "LoadCredential=admission:" in text
        assert "ConditionPathExists=" in text
        statuses = [
            line.partition("=")[2].split()
            for line in text.splitlines()
            if line.startswith("RestartPreventExitStatus=")
        ]
        assert statuses and "78" in statuses[0]
    assert "LoadCredential=admission:@ADMITTED@" in template
    assert "LoadCredential=admission:/etc/tessera/reasoning.admitted" in UNIT.read_text()


def test_in_unit_readiness_forwards_admitted_path():
    props = riu.unit_properties(UNIT.read_text())
    assert props["admitted_path"] == "/etc/tessera/reasoning.admitted"
    seen = []

    def runner(argv):
        seen.append(argv)
        return 0, "{}"

    present = lambda path: os.lstat("/")  # noqa: E731 - every credential source "exists"
    riu.run_in_unit(UNIT, state_dir="/s", runner=runner, lstat=present)
    riu.run_in_unit(UNIT, state_dir="/s", admitted_path="/x/m", runner=runner, lstat=present)
    for argv, expected in zip(seen, ("/etc/tessera/reasoning.admitted", "/x/m"), strict=True):
        assert argv[argv.index("--admitted-path") + 1] == expected
