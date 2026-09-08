from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_execution import _grant_payload, _setup

from tessera_control.runtime_packages import RuntimePackageStore
from tessera_control.skills import SkillStore
from tessera_control.store import (
    Conflict,
    Forbidden,
    NotFound,
    Store,
    canonical_digest,
    initialize_state,
)
from tessera_control.workspace import WorkspaceError, snapshot_skill


def _skill(tmp_path: Path, name: str = "team/safe"):
    root = tmp_path / name.replace("/", "-")
    root.mkdir()
    (root / "SKILL.md").write_text("---\nname: safe\ndescription: bounded test skill\n---\n")
    (root / "setup.py").write_text("raise RuntimeError('must never run')\n")
    return snapshot_skill(root, qualified_name=name)


def _release_body(skill: dict[str, object], worker: dict[str, object]) -> dict[str, object]:
    return {
        "expected_digest": skill["digest"],
        "worker_principal_id": worker["id"],
        "permits_worker_use": True,
        "rationale": "A human reviewed this synthetic instruction-only skill.",
        "expires_at": (datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
    }


def test_skill_snapshot_is_metadata_only_immutable_and_nonexecuting(tmp_path: Path) -> None:
    state = tmp_path / "state"
    initialize_state(state)
    store = SkillStore(state)
    owner = store.authenticate((state / "owner.header").read_text().split("Bearer ", 1)[1].strip())
    project_body = {"name": "Skills"}
    _, project = store.create_project(
        owner, "project", canonical_digest(project_body), project_body
    )
    skill = _skill(tmp_path)
    marker = tmp_path / "executed"
    (tmp_path / "team-safe" / "setup.py").write_text(f"open({marker!r}, 'w').write('bad')")
    skill = snapshot_skill(tmp_path / "team-safe", qualified_name="team/safe")
    first = store.snapshot_skill(owner, project["id"], "skill", skill)
    assert first == store.snapshot_skill(owner, project["id"], "skill", skill)
    assert not marker.exists()
    assert first[1]["digest"] == skill.digest
    assert first[1]["manifest"] == [
        {"relative_path": path, "sha256": digest, "size": size}
        for path, digest, size in skill.manifest
    ]
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM skill_snapshots").fetchone()[0] == 1
        event_count = connection.execute(
            "SELECT count(*) FROM events WHERE event_type='skill.snapshot_persisted'"
        ).fetchone()[0]
        assert event_count == 1

    (tmp_path / "team-safe" / "link").symlink_to(marker)
    with pytest.raises(WorkspaceError, match="symlink"):
        snapshot_skill(tmp_path / "team-safe", qualified_name="team/safe")


def test_skill_releases_are_human_scoped_expiring_and_transactional(tmp_path: Path) -> None:
    state, _, owner, worker, project, _ = _setup(tmp_path)
    store = SkillStore(state)
    _, skill = store.snapshot_skill(owner, project["id"], "skill", _skill(tmp_path))
    body = _release_body(skill, worker)
    first = store.release_skill(owner, skill["id"], "release", canonical_digest(body), body)
    assert first == store.release_skill(owner, skill["id"], "release", canonical_digest(body), body)
    with pytest.raises(Conflict, match="different request"):
        changed = body | {"rationale": "changed"}
        store.release_skill(owner, skill["id"], "release", canonical_digest(changed), changed)
    with pytest.raises(Conflict, match="previous"):
        store.release_skill(owner, skill["id"], "second", canonical_digest(body), body)

    agent_body = {
        "kind": "agent",
        "audience": "control",
        "project_id": project["id"],
        "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
    }
    _, issued = Store(state).issue_credential(
        owner, "agent", canonical_digest(agent_body), agent_body, "z" * 43
    )
    agent = Store(state).authenticate("z" * 43)
    with pytest.raises(Forbidden, match="human"):
        store.revoke_skill_release(agent, first[1]["id"], "no", "no", 1)
    revoked = store.revoke_skill_release(owner, first[1]["id"], "revoke", "revoke", 1)
    assert revoked[1]["status"] == "revoked"
    with pytest.raises(Conflict, match="stale"):
        store.revoke_skill_release(owner, first[1]["id"], "again", "again", 1)
    assert issued["principal_id"] == agent["id"]


def test_pinned_skill_admission_binds_release_and_runtime_fails_closed(tmp_path: Path) -> None:
    state, execution, owner, worker, project, task = _setup(tmp_path)
    skill_store = SkillStore(state)
    _, skill = skill_store.snapshot_skill(owner, project["id"], "skill", _skill(tmp_path))
    with sqlite3.connect(state / "tessera.sqlite3") as connection:
        snapshot_id = connection.execute("SELECT snapshot_id FROM snapshot_releases").fetchone()[0]
        snapshot_digest = connection.execute("SELECT digest FROM source_snapshots").fetchone()[0]
    revision = {
        "expected_revision": task["revision"],
        "title": task["title"],
        "purpose": task["purpose"],
        "budget_cents": task["budget_cents"],
        "max_duration_seconds": task["max_duration_seconds"],
        "execution_spec": {
            "argv": ["python", "-c", "print('synthetic')"],
            "input_snapshot_digest": snapshot_digest,
            "pinned_skill_digests": [skill["digest"]],
            "output_paths": ["result.txt"],
            "profile": "local-cpu",
            "network_enabled": False,
            "max_output_bytes": 1024,
        },
    }
    control = Store(state)
    _, planned = control.revise_task_plan(
        owner, task["id"], "pinned-plan", canonical_digest(revision), revision
    )
    approval_body = {"expected_revision": planned["revision"]}
    _, approved = control.approve_task(
        owner,
        task["id"],
        "pinned-approve",
        canonical_digest(approval_body),
        planned["revision"],
        planned["plan_digest"],
    )
    payload = _grant_payload(approved, worker, {"id": snapshot_id})
    with pytest.raises(Forbidden, match="Skill has no live release"):
        execution.issue_execution_grant(
            owner, project["id"], "grant", canonical_digest(payload), payload
        )
    release_body = _release_body(skill, worker)
    _, release = skill_store.release_skill(
        owner, skill["id"], "release", canonical_digest(release_body), release_body
    )
    _, grant = execution.issue_execution_grant(
        owner, project["id"], "grant", canonical_digest(payload), payload
    )
    _, lease = execution.acquire_execution_lease(worker, grant["id"], "lease", "lease")
    with pytest.raises(Conflict, match="capabilities"):
        RuntimePackageStore(state).execution_package(
            worker, lease["id"], "package", "package", lease["epoch"]
        )
    skill_store.revoke_skill_release(owner, release["id"], "revoke", "revoke", 1)
    with pytest.raises(Forbidden, match="Skill has no live release"):
        execution.renew_execution_lease(worker, lease["id"], "renew", "renew", lease["epoch"])


def test_skill_cross_project_lookup_is_denied(tmp_path: Path) -> None:
    state = tmp_path / "state"
    initialize_state(state)
    store = SkillStore(state)
    owner = store.authenticate((state / "owner.header").read_text().split("Bearer ", 1)[1].strip())
    first_body, second_body = {"name": "One"}, {"name": "Two"}
    _, first = store.create_project(owner, "one", canonical_digest(first_body), first_body)
    _, second = store.create_project(owner, "two", canonical_digest(second_body), second_body)
    _, skill = store.snapshot_skill(owner, first["id"], "skill", _skill(tmp_path))
    agent_body = {
        "kind": "agent",
        "audience": "control",
        "project_id": second["id"],
        "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
    }
    store.issue_credential(owner, "agent", canonical_digest(agent_body), agent_body, "x" * 43)
    agent = store.authenticate("x" * 43)
    with pytest.raises(NotFound):
        store.get_skill_snapshot(agent, skill["id"])
    assert store.get_skill_snapshot(owner, skill["id"])["project_id"] == first["id"]
