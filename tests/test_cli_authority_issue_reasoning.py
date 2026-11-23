"""`tessera authority issue-reasoning` against the real in-process control app (ADR 0155).

The command is a thin client: the control plane decides who may issue, whether spend limits
exist and how long the grant may live. Synthetic credentials only; no network.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from test_reasoning_authority_grant import MODEL, Bare

from tessera_control import cli
from tessera_control.agent_research_review import LocalTransport
from tessera_control.authority_cli import reasoning_body, run_authority

pytestmark = pytest.mark.real_owner_trust  # the CLI's own label is what makes the owner acceptable


def _transport(world: Bare, seen: list):
    class InProcess(LocalTransport):
        """The CLI's headers (credential, idempotency key, identity) sent to the in-process app."""

        def __call__(self, method, path, body=None, key=None):
            headers = self.request_headers(key)
            seen.append((method, path, body, key, {k: v for k, v in headers.items()}))
            response = world.client.request(method, path, json=body, headers=headers)
            try:
                return response.status_code, response.json()
            except ValueError:
                return response.status_code, None

    return InProcess


@pytest.fixture
def world(tmp_path):
    world = Bare(tmp_path)
    world.header = tmp_path / "owner.header"
    world.header.write_text((world.state / "owner.header").read_text())
    world.header.chmod(0o600)
    return world


def _limits(world: Bare, provider="anthropic"):
    body = {"project_id": world.project, "provider_id": provider, "per_call_cents": 5}
    response = world.client.put(
        "/model-spend/limits",
        json=body | {"daily_cents": 20},
        headers={**world.owner, "X-Tessera-Automation": "test-operator", **world.key()},
    )
    assert response.status_code == 200, response.text


def _argv(world: Bare, *extra):
    return [
        "authority", "issue-reasoning",
        "--project", world.project,
        "--subject-credential-id", world.credential,
        "--destination", world.agent["X-Agent-Destination"],
        "--model", MODEL,
        "--provider", "anthropic",
        "--budget-cents", "25",
        "--expires-in", "3600",
        "--purpose", "plan critique",
        "--automation-label", "operator-first-call",
        "--owner-header", str(world.header),
        *extra,
    ]  # fmt: skip


def _run(world, capsys, *extra):
    seen: list = []
    code = run_authority(cli.parser().parse_args(_argv(world, *extra)), _transport(world, seen))
    out = capsys.readouterr()
    return code, out, seen


def _no_secret(world: Bare, out) -> None:
    owner_token = world.owner["Authorization"].removeprefix("Bearer ")
    for text in (out.out, out.err):
        assert owner_token not in text and world.token not in text and "Bearer" not in text


def test_issues_a_labelled_reasoning_grant_and_prints_the_runtime_profile(world, capsys):
    _limits(world)
    code, out, seen = _run(world, capsys)
    assert code == 0, out
    printed = json.loads(out.out)
    (grant,) = world.rows("SELECT * FROM agent_authority_grants")
    assert printed["grant_id"] == grant["id"]
    assert (printed["revision"], printed["epoch"]) == (grant["revision"], grant["epoch"])
    assert printed["kind"] == "reasoning" and printed["provider_id"] == "anthropic"
    assert printed["model"] == MODEL and printed["budget_cents"] == 25
    assert printed["remaining_budget_cents"] == 25 and printed["status"] == "active"
    assert printed["runtime_profile"] == {
        "grant_id": grant["id"],
        "revision": grant["revision"],
        "epoch": grant["epoch"],
        "subject_credential_id": world.credential,
        "destination": world.agent["X-Agent-Destination"],
    }
    _no_secret(world, out)
    # One POST on the normal route, with a fresh key and the CLI's automation label.
    ((method, path, body, key, headers),) = seen
    assert (method, path) == ("POST", f"/projects/{world.project}/agent-authority-grants")
    assert key.startswith("issue-reasoning-") and f"idempotency-key: {key}" in out.err
    assert headers["X-Tessera-Automation"] == "operator-first-call"
    assert body["bounds"]["kind"] == "reasoning" and body["bounds"]["bindings"] == []
    assert body["bounds"]["max_duration_seconds"] == 86400 and body["request_id"] is None
    bounds = json.loads(grant["bounds_json"])
    expires = datetime.fromisoformat(bounds["expires_at"].replace("Z", "+00:00"))
    assert timedelta(minutes=59) < expires - datetime.now(UTC) <= timedelta(hours=1)
    (event,) = world.events("agent_authority.issued")
    assert event["kind"] == "reasoning"
    assert event["automation_label"] == "operator-first-call"
    assert event["decision_class"] == "owner_automation_labelled"
    assert world.rows("SELECT * FROM execution_grants") == []


def test_a_retry_with_the_printed_key_and_expiry_replays_the_same_grant_once(world, capsys):
    _limits(world, provider="*")
    code, out, _ = _run(world, capsys)
    assert code == 0
    lines = dict(line.split(": ", 1) for line in out.err.strip().splitlines())
    key, expires_at = lines["idempotency-key"], lines["expires-at"]
    argv = [a for a in _argv(world) if a not in ("--expires-in", "3600")]
    argv += ["--idempotency-key", key, "--expires-at", expires_at]
    assert run_authority(cli.parser().parse_args(argv), _transport(world, [])) == 0
    again = capsys.readouterr()
    assert json.loads(again.out)["grant_id"] == json.loads(out.out)["grant_id"]
    assert len(world.rows("SELECT * FROM agent_authority_grants")) == 1
    # The same key with a new relative expiry is a different request: refused, nothing written.
    code, refused, _ = _run(world, capsys, "--idempotency-key", key)
    assert code == 1 and json.loads(refused.out)["status"] == 409
    assert len(world.rows("SELECT * FROM agent_authority_grants")) == 1


def test_expires_in_and_expires_at_are_exclusive(world):
    with pytest.raises(SystemExit):
        cli.parser().parse_args([*_argv(world), "--expires-at", "2030-01-01T00:00:00+00:00"])


def test_refused_with_409_until_spend_limits_exist(world, capsys):
    code, out, _ = _run(world, capsys)
    assert code == 1
    assert json.loads(out.out) == {"detail": "model_spend_not_configured", "status": 409}
    assert world.rows("SELECT * FROM agent_authority_grants") == []
    _no_secret(world, out)


def test_the_owner_without_an_automation_label_is_refused(world):
    """The CLI always labels; the same request without the label is refused by the platform."""
    _limits(world)
    arguments = cli.parser().parse_args(_argv(world))
    seen: list = []
    transport = _transport(world, seen)("http://127.0.0.1:8765", world.owner["Authorization"])
    path = f"/projects/{world.project}/agent-authority-grants"
    status, _ = transport("POST", path, reasoning_body(arguments), "unlabelled-1")
    assert status == 403 and "X-Tessera-Automation" not in seen[0][4]
    assert world.rows("SELECT * FROM agent_authority_grants") == []


def test_an_agent_credential_cannot_issue_through_the_cli(world, tmp_path, capsys):
    _limits(world)
    agent_header = tmp_path / "agent.header"
    agent_header.write_text(f"Authorization: Bearer {world.token}\n")
    agent_header.chmod(0o600)
    code, out, _ = _run(world, capsys, "--owner-header", str(agent_header))
    assert code == 1 and json.loads(out.out)["status"] in {401, 403}
    assert world.rows("SELECT * FROM agent_authority_grants") == []
    _no_secret(world, out)


@pytest.mark.parametrize(
    "extra",
    [
        ("--expires-in", "86401"),
        ("--expires-in", "0"),
        ("--budget-cents", "0"),
        ("--provider", "*"),
        ("--max-duration-seconds", "86401"),
    ],
)
def test_invalid_arguments_send_nothing(world, capsys, extra):
    _limits(world)
    code, out, seen = _run(world, capsys, *extra)
    assert code == 2 and seen == [] and out.out == ""
    assert world.rows("SELECT * FROM agent_authority_grants") == []


def test_the_command_is_wired_into_main(world, monkeypatch, capsys):
    monkeypatch.setattr(
        "tessera_control.authority_cli._transport_class", lambda: _transport(world, [])
    )
    _limits(world)
    assert cli.main(_argv(world)) == 0
    assert json.loads(capsys.readouterr().out)["kind"] == "reasoning"
