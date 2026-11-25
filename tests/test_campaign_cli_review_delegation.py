"""`tessera campaign approve --review-delegation-id` sends the field; without it the body is
unchanged (ADR 0150 amendment 2026-11-25)."""

from __future__ import annotations

from test_campaign_cli import DIGEST, FakeTransport, _run, owner_header  # noqa: F401


def _approve(capsys, owner_header, *extra):  # noqa: F811
    FakeTransport.calls = []
    FakeTransport.reply = (200, {"id": "cmp_1", "status": "running"})
    code, _out = _run(
        [
            "approve",
            "--campaign-id",
            "cmp_1",
            "--expected-revision",
            "1",
            "--expected-spec-digest",
            DIGEST,
            "--automation-label",
            "test-label",
            "--owner-header",
            str(owner_header),
            *extra,
        ],
        capsys,
    )
    assert code == 0
    ((method, path, body, _key, _auth),) = FakeTransport.calls
    assert (method, path) == ("POST", "/campaigns/cmp_1/approve")
    return body


def test_the_flag_names_the_review_delegation(capsys, owner_header):  # noqa: F811
    body = _approve(capsys, owner_header, "--review-delegation-id", "deleg_abc")
    assert body["review_delegation_id"] == "deleg_abc"


def test_without_the_flag_the_body_has_no_such_field(capsys, owner_header):  # noqa: F811
    body = _approve(capsys, owner_header)
    assert "review_delegation_id" not in body
    assert set(body) == {"expected_revision", "expected_spec_digest"}
