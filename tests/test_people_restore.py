"""Restore and people (ADR 0084 'Restore'; docs/people-enrollment.md). Synthetic only.

A restored state is a held copy: every restored principal and credential is revoked and a new
self-rooted owner is minted, so pre-restore roots survive only as recorded rows.
"""

import secrets
import sqlite3

from fastapi.testclient import TestClient
from test_review_jobs import AUTO, enrol_person, resolve_body
from test_review_separation import build

from tessera_control.api import create_app
from tessera_control.recovery import backup_state, restore_state


def _bearer(header_file):
    return {"Authorization": header_file.read_text().strip().split(": ", 1)[1]}


def test_restore_revokes_old_identities_and_mints_a_distinct_owner_root(tmp_path):
    w = build(tmp_path)
    person = enrol_person(w.client, w.owner)
    _, posted = w.ingest()  # owner-enrolled agent submits; a second-root reviewer agent posts
    assert posted.status_code == 201, posted.text
    finding = posted.json()["findings"][0]

    backup = tmp_path / "backup"
    backup_state(w.state, backup)
    restored = tmp_path / "restored"
    restore_state(backup, restored)
    client = TestClient(create_app(restored))
    new_owner = _bearer(restored / "owner.header")

    # (b) every pre-restore credential is revoked: owner, enrolled person, agents
    for old in (w.owner, person, w.submitter, w.reviewer, w.reviewer_root):
        assert client.get("/projects", headers=old).status_code == 401
    assert client.get("/projects", headers=new_owner).status_code == 200

    with sqlite3.connect(restored / "tessera.sqlite3") as db:
        # enrolled_people survives as recorded rows, none of them live
        rows = db.execute(
            "SELECT p.revoked_at IS NOT NULL FROM enrolled_people e "
            "JOIN principals p ON p.id=e.principal_id"
        ).fetchall()
        assert rows == [(1,)]
        (new_id,) = [r[0] for r in db.execute("SELECT id FROM principals WHERE revoked_at IS NULL")]
        assert (
            db.execute(
                "SELECT COUNT(*) FROM enrolled_people WHERE principal_id=?", (new_id,)
            ).fetchone()[0]
            == 0
        )
        old_owner_roots = {
            r[0]
            for r in db.execute(
                "SELECT principal_id FROM credentials "
                "WHERE kind='local_owner' AND revoked_at IS NOT NULL"
            )
        }
        assert new_id not in old_owner_roots and old_owner_roots

    # (c) the new owner is a different root; a pre-restore job's findings cannot be resolved by it
    # in default mode while control is held (writes are refused), and the pre-restore rows persist
    attempt = client.post(
        f"/review-findings/{finding['id']}/resolve",
        headers={**new_owner, **AUTO, "Idempotency-Key": secrets.token_hex(8)},
        json=resolve_body(finding),
    )
    assert attempt.status_code == 403, attempt.text
    assert "held after restore" in attempt.json()["detail"]  # the hold, not the person rule
    with sqlite3.connect(restored / "tessera.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM review_finding_decisions").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM review_findings").fetchone()[0] == 1
