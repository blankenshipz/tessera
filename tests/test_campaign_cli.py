"""`tessera campaign ...` client commands: paths, bodies, JSON output and exit codes."""

from __future__ import annotations

import json
import sys

import pytest

from tessera_control import cli

DIGEST = "b" * 64


class FakeTransport:
    calls: list[tuple] = []
    reply: tuple[int, object] = (201, {"id": "cmp_1", "status": "submitted"})

    def __init__(self, origin, authorization):
        self.origin, self.authorization = origin, authorization

    def __call__(self, method, path, body=None, key=None):
        FakeTransport.calls.append((method, path, body, key, self.authorization))
        return FakeTransport.reply


@pytest.fixture(autouse=True)
def _reset():
    FakeTransport.calls = []
    FakeTransport.reply = (201, {"id": "cmp_1", "status": "submitted"})


@pytest.fixture
def owner_header(tmp_path):
    path = tmp_path / "owner.header"
    path.write_text("Authorization: Bearer synthetic-owner\n")
    path.chmod(0o600)
    return path


def _run(argv, capsys):
    arguments = cli.parser().parse_args(["campaign", *argv])
    code = cli.run_campaign(arguments, FakeTransport)
    return code, capsys.readouterr()


def _spec_file(tmp_path):
    spec = {
        "version": 1,
        "name": "n",
        "budget_cap_cents": 2,
        "deadline_seconds": 600,
        "worker_principal_id": "worker",
        "stages": [
            {
                "key": "s1",
                "backend": "docker-local-cpu-v1",
                "image": "python@sha256:" + "a" * 64,
                "argv": ["python", "-c", "print(1)"],
                "output_paths": ["o.json"],
                "max_output_bytes": 10,
                "max_duration_seconds": 60,
                "inputs": [
                    {
                        "kind": "snapshot",
                        "name": "code",
                        "snapshot_id": "snap",
                        "digest": DIGEST,
                        "data_class": "public",
                    }
                ],
            }
        ],
    }
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec))
    return path


def test_submit_posts_spec_with_idempotency_key(tmp_path, owner_header, capsys):
    code, out = _run(
        ["submit", "--project-id", "p1", "--spec", str(_spec_file(tmp_path)),
         "--idempotency-key", "k1", "--owner-header", str(owner_header)],
        capsys,
    )  # fmt: skip
    method, path, body, key, auth = FakeTransport.calls[0]
    assert (code, method, path, key) == (0, "POST", "/projects/p1/campaigns", "k1")
    assert body["spec"]["name"] == "n" and auth == "Bearer synthetic-owner"
    assert json.loads(out.out) == {"id": "cmp_1", "status": "submitted"}
    assert "synthetic-owner" not in out.out + out.err


def test_submit_rejects_invalid_spec_without_calling(tmp_path, owner_header, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("{}")
    code, out = _run(
        ["submit", "--project-id", "p", "--spec", str(bad), "--idempotency-key", "k",
         "--owner-header", str(owner_header)],
        capsys,
    )  # fmt: skip
    assert code == 1 and not FakeTransport.calls and "Invalid campaign spec" in out.err


def test_approve_requires_the_exact_digest_and_labels_itself_by_default(owner_header, capsys):
    base = ["approve", "--campaign-id", "c1", "--expected-revision", "1",
            "--idempotency-key", "k", "--owner-header", str(owner_header)]  # fmt: skip
    with pytest.raises(SystemExit):  # the digest is mandatory; the label no longer is
        cli.parser().parse_args(["campaign", *base])
    code, _ = _run([*base, "--expected-spec-digest", DIGEST], capsys)
    method, path, body, *_ = FakeTransport.calls[0]
    assert (code, method, path) == (0, "POST", "/campaigns/c1/approve")
    assert body == {"expected_revision": 1, "expected_spec_digest": DIGEST}


def test_reads_cancel_and_failure_exit_code(owner_header, capsys):
    common = ["--owner-header", str(owner_header)]
    _run(["get", "--campaign-id", "c1", *common], capsys)
    _run(["receipt", "--campaign-id", "c1", *common], capsys)
    _run(["list", "--project-id", "p", "--limit", "5", *common], capsys)
    _run(["cancel", "--campaign-id", "c1", "--expected-revision", "3", "--reason", "stop",
          "--idempotency-key", "k", *common], capsys)  # fmt: skip
    assert [(c[0], c[1]) for c in FakeTransport.calls] == [
        ("GET", "/campaigns/c1"),
        ("GET", "/campaigns/c1/receipt"),
        ("GET", "/projects/p/campaigns?offset=0&limit=5"),
        ("POST", "/campaigns/c1/cancel"),
    ]
    assert FakeTransport.calls[3][2] == {"expected_revision": 3, "reason": "stop"}
    FakeTransport.reply = (404, {"detail": "Not found"})
    code, out = _run(["get", "--campaign-id", "x", *common], capsys)
    assert code == 1 and json.loads(out.out) == {"status": 404, "body": {"detail": "Not found"}}


def test_non_loopback_url_is_refused(owner_header, capsys):
    code, out = _run(
        ["get", "--campaign-id", "c", "--owner-header", str(owner_header),
         "--control-url", "http://example.com:80"],
        capsys,
    )  # fmt: skip
    assert code == 1 and not FakeTransport.calls and "loopback" in out.err


def test_serve_flags_default_and_override(monkeypatch):
    base = ["tessera", "serve", "--state-dir", "/x", "--port", "1"]
    parsed = cli.parser().parse_args(base[1:])
    assert parsed.campaign_tick_seconds == 2.0 and parsed.no_campaign_runner is False
    parsed = cli.parser().parse_args(
        [*base[1:], "--campaign-tick-seconds", "0.5", "--no-campaign-runner"]
    )
    assert parsed.campaign_tick_seconds == 0.5 and parsed.no_campaign_runner is True
    assert sys.argv is not None


def test_campaign_standing_cli_round_trip(tmp_path, owner_header, capsys):
    spec_path = _spec_file(tmp_path)
    common = ["--owner-header", str(owner_header)]
    code, out = _run(
        ["standing", "create", "--project-id", "p1", "--spec", str(spec_path),
         "--expected-spec-digest", DIGEST, "--per-start-budget-cents", "2",
         "--max-starts", "3", "--max-total-budget-cents", "6",
         "--not-after", "2030-01-01T00:00:00Z", "--min-interval-seconds", "600",
         "--retry-max", "1", "--retry-on", "failed_or_stopped",
         "--automation-label", "ops", "--idempotency-key", "k1", *common],
        capsys,
    )  # fmt: skip
    assert code == 0
    method, path, body, key, _ = FakeTransport.calls[-1]
    assert (method, path, key) == ("POST", "/projects/p1/standing-approvals", "k1")
    assert body["per_start_budget_cap_cents"] == 2 and body["retry_on"] == "failed_or_stopped"
    assert body["trigger"] == "schedule" and body["min_interval_seconds"] == 600
    _run(["standing", "list", "--project-id", "p1", *common], capsys)
    _run(["standing", "show", "--project-id", "p1", "--id", "s1", *common], capsys)
    assert [c[:2] for c in FakeTransport.calls[-2:]] == [
        ("GET", "/projects/p1/standing-approvals?offset=0&limit=50"),
        ("GET", "/projects/p1/standing-approvals/s1"),
    ]
    FakeTransport.calls.clear()
    FakeTransport.reply = (200, {"running_campaign_ids": ["c9"], "revision": 2})
    code, out = _run(
        ["standing", "revoke", "--project-id", "p1", "--id", "s1", "--expected-revision", "1",
         "--reason", "done", "--cancel-running", "--idempotency-key", "k2", *common],
        capsys,
    )  # fmt: skip
    assert code == 0
    assert [c[:2] for c in FakeTransport.calls] == [
        ("POST", "/projects/p1/standing-approvals/s1/revoke"),
        ("GET", "/campaigns/c9"),
        ("POST", "/campaigns/c9/cancel"),
    ]
    assert json.loads(out.out)["cancelled"][0]["campaign_id"] == "c9"
