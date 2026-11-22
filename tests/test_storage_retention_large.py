from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from test_large_artifacts import (  # noqa: F401 - the two autouse fixtures
    _age,
    _commit_reject_without_reclaim,
    _completed,
    _counts,
    _events,
    _expire_lease,
    _fresh_operator_limits,
    _manifest_for,
    tiny_thresholds,
)

from tessera_control import storage_retention
from tessera_control.storage_retention_large import (
    LargeReclaimError,
    delete_large,
    list_large_candidates,
)
from tessera_control.storage_retention_models import POLICY_FILE, RetentionPolicy
from tessera_control.store import _connect

NOW = datetime.now(UTC)
ON = RetentionPolicy(reclaim_orphaned_large_artifacts=True)


def _sub(tmp_path, name):
    (tmp_path / name).mkdir()
    return tmp_path / name


def _orphan(tmp_path):
    state, worker, lease, store, artifact = _completed(tmp_path)
    _expire_lease(state, lease["id"])
    _age(state, artifact["id"])
    return state, worker, lease, store, artifact


def _candidate(state, ref):
    found, skipped = list_large_candidates(state, ON, now=NOW)
    assert skipped == []
    return next(c for c in found if c.ref == ref)


def test_an_old_unmanifested_artifact_is_listed_for_deletion(tmp_path):
    state, _, _, _, artifact = _orphan(tmp_path)
    found, skipped = list_large_candidates(state, ON, now=NOW)
    assert skipped == []
    [candidate] = found
    assert (candidate.kind, candidate.ref, candidate.decision) == (
        "large_artifact",
        artifact["id"],
        "delete",
    )
    assert candidate.reasons == ["unmanifested"]
    assert (candidate.size_bytes, candidate.sha256) == (artifact["size"], artifact["sha256"])
    assert candidate.path == artifact["relative_path"]
    assert candidate.project_id == artifact["project_id"]


def test_listing_is_opt_in(tmp_path):
    state, *_ = _orphan(tmp_path)
    assert list_large_candidates(state, RetentionPolicy(), now=NOW) == ([], [])


def test_manifested_live_and_young_artifacts_are_not_listed(tmp_path):
    state, worker, lease, store, artifact = _completed(_sub(tmp_path, "young"))
    _expire_lease(state, lease["id"])  # over, but not old enough
    assert list_large_candidates(state, ON, now=NOW) == ([], [])

    state, worker, lease, store, artifact = _completed(_sub(tmp_path, "live"))
    _age(state, artifact["id"])  # old, but its lease may still post a manifest
    assert list_large_candidates(state, ON, now=NOW) == ([], [])

    state, worker, lease, store, artifact = _completed(_sub(tmp_path, "undecided"))
    _manifest_for(state, worker, lease, store, artifact)
    _expire_lease(state, lease["id"])
    _age(state, artifact["id"])
    assert list_large_candidates(state, ON, now=NOW) == ([], [])


def test_a_rejected_manifest_is_listed_as_rejected(tmp_path):
    state, worker, lease, store, artifact = _completed(tmp_path)
    manifest = _manifest_for(state, worker, lease, store, artifact)
    _commit_reject_without_reclaim(state, manifest)
    [candidate] = list_large_candidates(state, ON, now=NOW)[0]
    assert candidate.reasons == ["rejected"]


def test_nothing_is_listed_while_control_is_held(tmp_path):
    state, *_ = _orphan(tmp_path)
    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute("UPDATE control_recovery SET state='held_restore' WHERE singleton=1")
    found, skipped = list_large_candidates(state, ON, now=NOW)
    assert found == [] and [s["kind"] for s in skipped] == ["large_artifact"]


def test_delete_frees_the_chunks_and_records_the_event(tmp_path):
    state, _, _, _, artifact = _orphan(tmp_path)
    candidate = _candidate(state, artifact["id"])
    assert _counts(state, artifact["id"]) == (32, 1)
    result = delete_large(state, candidate, run_id="run")
    assert result == {
        "ref": artifact["id"],
        "size_bytes": artifact["size"],
        "sha256": artifact["sha256"],
        "state": "reclaimed",
    }
    assert _counts(state, artifact["id"]) == (0, 0)
    with _connect(state / "tessera.sqlite3") as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM artifacts WHERE id=?", (artifact["id"],)
            ).fetchone()[0]
            == 1
        )
    [event] = _events(state)
    assert (event["artifact_id"], event["reason"], event["trigger"]) == (
        artifact["id"],
        "unmanifested",
        "retention",
    )
    with pytest.raises(LargeReclaimError):  # nothing left to reclaim
        delete_large(state, candidate, run_id="run")
    assert len(_events(state)) == 1


def test_delete_refuses_when_it_gained_a_manifest_after_planning(tmp_path):
    state, worker, lease, store, artifact = _orphan(tmp_path)
    candidate = _candidate(state, artifact["id"])
    _manifest_for(state, worker, lease, store, artifact)
    with pytest.raises(LargeReclaimError):
        delete_large(state, candidate, run_id="run")
    assert _counts(state, artifact["id"]) == (32, 1)
    assert _events(state) == []


def test_delete_refuses_a_candidate_that_no_longer_matches(tmp_path):
    state, _, _, _, artifact = _orphan(tmp_path)
    candidate = _candidate(state, artifact["id"])
    from dataclasses import replace

    with pytest.raises(LargeReclaimError, match="no longer matches"):
        delete_large(state, replace(candidate, sha256="0" * 64), run_id="run")
    assert _counts(state, artifact["id"]) == (32, 1)


def test_run_dry_run_then_apply_deletes_it_with_a_receipt(tmp_path):
    state, _, _, _, artifact = _orphan(tmp_path)
    (state / POLICY_FILE).write_text(json.dumps({"reclaim_orphaned_large_artifacts": True}))
    dry = storage_retention.run(state, mode="dry_run", trigger="manual")
    assert dry["deleted"] == [] and _counts(state, artifact["id"]) == (32, 1)
    receipt = storage_retention.run(
        state, mode="apply", trigger="manual", expect_plan_digest=dry["plan_digest"]
    )
    assert receipt["status"] == "complete" and receipt["failed"] == []
    [deleted] = receipt["deleted"]
    assert (deleted["kind"], deleted["ref"]) == ("large_artifact", artifact["id"])
    assert (deleted["size_bytes"], deleted["sha256"]) == (artifact["size"], artifact["sha256"])
    assert receipt["bytes_freed"] == artifact["size"]
    assert _counts(state, artifact["id"]) == (0, 0)


def test_run_without_opt_in_selects_nothing(tmp_path):
    state, *_ = _orphan(tmp_path)
    (state / POLICY_FILE).write_text(json.dumps({"artifact_max_age_days": 30}))
    assert (
        storage_retention.run(state, mode="dry_run", trigger="manual")["plan"]["candidates"] == []
    )
