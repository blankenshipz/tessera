"""Isolated enrollment tests: temporary dummy owner reference, fake HTTP only."""

import importlib.util
import json
import os
from argparse import Namespace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tessera_control.models import CreateCredential, CreateProject

SPEC = importlib.util.spec_from_file_location(
    "enroll_local_mcp", Path(__file__).parents[1] / "scripts/enroll_local_mcp.py"
)
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)


@pytest.fixture
def args(tmp_path):
    root = tmp_path.resolve()
    owner = root / "dummy-owner.header"
    owner.write_text("Authorization: Bearer " + "o" * 64 + "\n")
    owner.chmod(0o600)
    return Namespace(
        control_url="http://127.0.0.1:8765",
        owner_header=owner,
        private_dir=root / "enrollment",
        project_id=None,
        create_project="Local research",
        destination="opencode-local",
        duration_minutes=60,
        allow_proposals=False,
        resume=False,
    )


class FakeHTTP:
    def __init__(self):
        self.calls = []
        self.lost = None
        self.projects = []

    def factory(self, origin, owner):
        assert origin == "http://127.0.0.1:8765"
        assert owner == "Bearer " + "o" * 64
        return self

    def __call__(self, method, path, body=None, key=None):
        self.calls.append((method, path, body, key))
        if method == "GET":
            return (
                {"projects": self.projects}
                if path.startswith("/projects?")
                else {"id": "project_1"}
            )
        assert key and len(key) == 48
        payload = json.loads(body)
        if path == "/projects":
            CreateProject.model_validate(payload)
            response = {"id": "project_1"}
        else:
            CreateCredential.model_validate(payload)
            response = {
                **payload,
                "credential_id": "credential_1",
                "principal_id": "principal_1",
                "revoked_at": None,
            }
        if self.lost == path:
            self.lost = None
            raise helper.EnrollmentError("Simulated ambiguous response")
        return response


def test_success_private_distinct_read_only_and_repeat_requires_resume(args, capsys):
    fake = FakeHTTP()
    result = helper.enroll(args, fake.factory)
    assert set(result) == {
        "project_id",
        "principal_id",
        "credential_id",
        "expires_at",
        "token_file",
    }
    token = Path(result["token_file"]).read_text()
    assert len(token) >= 43 and token != "o" * 64
    assert set(token) <= set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
    assert args.private_dir.stat().st_mode & 0o777 == 0o700
    for file in args.private_dir.iterdir():
        assert file.stat().st_mode & 0o777 == 0o600
    payload = json.loads(fake.calls[-1][2])
    assert payload["external_agent"] == {
        "actions": ["context:read", "proposal:read", "notebook:read"],
        "destination": "opencode-local",
    }
    assert (
        datetime.now(UTC)
        < datetime.fromisoformat(result["expires_at"])
        <= datetime.now(UTC) + timedelta(hours=24)
    )
    with pytest.raises(FileExistsError):
        helper.enroll(args, fake.factory)
    args.resume = True
    before = len(fake.calls)
    assert helper.enroll(args, fake.factory) == result
    assert len(fake.calls) == before  # Completed enrollment does not renew/reissue.
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("path", ["/projects", "/credentials"])
def test_uncertain_response_replays_exact_key_body_identity(args, path):
    fake = FakeHTTP()
    fake.lost = path
    with pytest.raises(helper.EnrollmentError):
        helper.enroll(args, fake.factory)
    original = next(call for call in fake.calls if call[1] == path)
    args.resume = True
    result = helper.enroll(args, fake.factory)
    replayed = [call for call in fake.calls if call[1] == path]
    assert replayed == [original, original]
    assert result["credential_id"] == "credential_1"


def test_existing_name_requires_explicit_id(args):
    fake = FakeHTTP()
    fake.projects = [{"id": "project_other", "name": args.create_project}]
    args.create_project = "  Local research  "
    with pytest.raises(helper.EnrollmentError, match="select"):
        helper.enroll(args, fake.factory)
    assert all(call[0] == "GET" for call in fake.calls)


def test_unknown_existing_state_requires_reconciliation(args):
    args.private_dir.mkdir(mode=0o700)
    (args.private_dir / "unrecognized").write_text("not enrollment state")
    args.resume = True
    fake = FakeHTTP()
    with pytest.raises(helper.EnrollmentError, match="Unexpected"):
        helper.enroll(args, fake.factory)
    assert not fake.calls


def test_select_project_and_optional_proposals(args):
    args.project_id, args.create_project, args.allow_proposals = "project_1", None, True
    fake = FakeHTTP()
    helper.enroll(args, fake.factory)
    assert fake.calls[0][:2] == ("GET", "/projects/project_1")
    actions = json.loads(fake.calls[-1][2])["external_agent"]["actions"]
    assert actions == ["context:read", "proposal:read", "notebook:read", "proposal:create"]


@pytest.mark.parametrize("change", ["expiry", "destination", "symlink", "permissions", "hardlink"])
def test_resume_fails_closed_without_network(args, change):
    fake = FakeHTTP()
    helper.enroll(args, fake.factory)
    args.resume = True
    intent = args.private_dir / "intent.json"
    if change == "expiry":
        value = json.loads(intent.read_text())
        value["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        intent.write_text(json.dumps(value))
    elif change == "destination":
        args.destination = "different"
    elif change == "permissions":
        intent.chmod(0o644)
    elif change == "symlink":
        saved = args.private_dir / "saved"
        intent.rename(saved)
        intent.symlink_to(saved)
    else:
        os.link(intent, args.private_dir / "linked")
    before = len(fake.calls)
    with pytest.raises((helper.EnrollmentError, OSError)):
        helper.enroll(args, fake.factory)
    assert len(fake.calls) == before


@pytest.mark.parametrize(
    "url", ["http://example.com", "http://127.0.0.1:8765/path", "http://user@127.0.0.1"]
)
def test_remote_or_credentialed_origin_rejected_before_owner_read(args, url):
    args.control_url = url
    args.owner_header.unlink()
    with pytest.raises(helper.EnrollmentError, match="local-only"):
        helper.enroll(args)


def test_main_error_never_echoes_sensitive_exception(args, monkeypatch, capsys):
    def fail(*_):
        raise RuntimeError("secret owner or response must never be printed")

    monkeypatch.setattr(helper, "enroll", fail)
    assert (
        helper.main(
            [
                "--owner-header",
                str(args.owner_header),
                "--private-dir",
                str(args.private_dir),
                "--create-project",
                "Local",
                "--destination",
                "local",
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert not captured.out
    assert "secret owner" not in captured.err


def test_no_redirect_or_proxy_forwarding():
    with pytest.raises(helper.EnrollmentError, match="Redirect"):
        helper.NoRedirect().redirect_request(None, None, 302, "", {}, "http://elsewhere")
