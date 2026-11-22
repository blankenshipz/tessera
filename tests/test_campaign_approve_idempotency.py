"""Campaign approval is safe for batch clients: a reused key never passes as a fresh approval."""

from __future__ import annotations

import pytest
from campaign_fixture import Env
from fastapi.testclient import TestClient

from tessera_control import cli
from tessera_control.api import create_app
from tessera_control.store import Conflict, canonical_digest


def _body(campaign: dict, **changes) -> dict:
    return {
        "expected_revision": campaign["revision"],
        "expected_spec_digest": campaign["spec_digest"],
    } | changes


def _approve(env: Env, campaign: dict, key: str, **changes):
    body = _body(campaign, **changes)
    return env.store.approve_campaign(
        env.human(), campaign["id"], key, canonical_digest(body), body
    )


def _two(env: Env) -> tuple[dict, dict]:
    return env.submit(), env.submit()


def test_a_key_reused_for_another_campaign_is_refused_and_applies_nothing(tmp_path):
    env = Env(tmp_path)
    first, second = _two(env)
    status, approved = _approve(env, first, "batch-key")
    assert status == 200 and approved["status"] == "running"
    with pytest.raises(Conflict, match="different campaign"):
        _approve(env, second, "batch-key")
    row = env.query("SELECT status FROM campaigns WHERE id=?", second["id"])[0]
    assert row["status"] == "submitted"  # loudly refused, not silently skipped or applied
    assert not env.query("SELECT 1 FROM campaign_approvals WHERE campaign_id=?", second["id"])


def test_fresh_keys_approve_every_campaign_of_a_batch(tmp_path):
    env = Env(tmp_path)
    first, second = _two(env)
    assert _approve(env, first, "k-1")[1]["status"] == "running"
    assert _approve(env, second, "k-2")[1]["status"] == "running"


def test_the_same_key_and_campaign_with_another_body_is_refused(tmp_path):
    env = Env(tmp_path)
    first, _ = _two(env)
    _approve(env, first, "k")
    with pytest.raises(Conflict, match="different request"):
        _approve(env, first, "k", expected_revision=first["revision"] + 1)


def test_an_identical_retry_replays_the_same_answer_once(tmp_path):
    env = Env(tmp_path)
    first, _ = _two(env)
    one = _approve(env, first, "k")
    assert _approve(env, first, "k") == one
    assert len(env.query("SELECT 1 FROM campaign_approvals WHERE campaign_id=?", first["id"])) == 1


def test_the_http_route_answers_409_for_a_reused_key(tmp_path):
    env = Env(tmp_path)
    first, second = _two(env)
    client = TestClient(create_app(env.state))
    token = (env.state / "owner.header").read_text().split("Bearer ", 1)[1].strip()

    def post(campaign: dict, key: str):
        return client.post(
            f"/campaigns/{campaign['id']}/approve",
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": key,
                "X-Tessera-Automation": "batch-test",
            },
            json=_body(campaign),
        )

    assert post(first, "same").status_code == 200
    refused = post(second, "same")
    assert refused.status_code == 409
    assert "different campaign" in refused.json()["detail"]
    assert post(second, "fresh").status_code == 200


# -- CLI: a fresh key per call, by campaign id --------------------------------------------


class _Transport:
    calls: list[tuple] = []

    def __init__(self, origin, authorization):
        pass

    def __call__(self, method, path, body=None, key=None):
        _Transport.calls.append((method, path, body, key))
        if method == "GET":
            return 200, {"id": "c1", "revision": 4}
        return 200, {"id": "c1", "status": "approved"}


@pytest.fixture
def owner_header(tmp_path):
    path = tmp_path / "owner.header"
    path.write_text("Authorization: Bearer synthetic-owner\n")
    path.chmod(0o600)
    _Transport.calls = []
    return path


def _cli(owner_header, capsys, *extra):
    argv = [
        "campaign", "approve", "--campaign-id", "c1", "--expected-spec-digest", "b" * 64,
        "--automation-label", "batch", "--owner-header", str(owner_header), *extra,
    ]  # fmt: skip
    code = cli.run_campaign(cli.parser().parse_args(argv), _Transport)
    return code, capsys.readouterr()


def test_the_cli_uses_a_fresh_key_per_call_and_reads_the_revision(owner_header, capsys):
    _cli(owner_header, capsys)
    _cli(owner_header, capsys)
    posts = [c for c in _Transport.calls if c[0] == "POST"]
    assert len(posts) == 2 and posts[0][3] != posts[1][3]
    assert all(key.startswith("approve-") for _, _, _, key in posts)
    assert posts[0][2] == {"expected_revision": 4, "expected_spec_digest": "b" * 64}


def test_an_explicit_key_and_revision_are_used_as_given(owner_header, capsys):
    code, out = _cli(owner_header, capsys, "--idempotency-key", "mine", "--expected-revision", "9")
    assert code == 0 and "idempotency-key" not in out.err
    assert _Transport.calls == [
        (
            "POST",
            "/campaigns/c1/approve",
            {"expected_revision": 9, "expected_spec_digest": "b" * 64},
            "mine",
        )
    ]
