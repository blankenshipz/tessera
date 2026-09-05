from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tessera_control.interaction import InteractionStore
from tessera_control.store import (
    Conflict,
    Forbidden,
    NotFound,
    Store,
    _connect,
    _token_hash,
    initialize_state,
)


def _future(hours: int = 1) -> str:
    return (datetime.now(UTC) + timedelta(hours=hours)).isoformat().replace("+00:00", "Z")


def _owner_token(state: Path) -> str:
    return (state / "owner.header").read_text().strip().split(": ", 1)[1].removeprefix("Bearer ")


def _setup(tmp_path: Path) -> tuple[Path, InteractionStore, dict[str, object], str]:
    state = tmp_path / "state"
    initialize_state(state)
    store = Store(state)
    owner = store.authenticate(_owner_token(state))
    project_id = "project-test"
    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            "INSERT INTO projects VALUES (?, 'org_local', 'Test', '2026-01-01T00:00:00Z')",
            (project_id,),
        )
    return state, InteractionStore(store), owner, project_id


def _project(state: Path, project_id: str) -> None:
    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            "INSERT INTO projects VALUES (?, 'org_local', ?, '2026-01-01T00:00:00Z')",
            (project_id, project_id),
        )


def _principal(
    state: Path,
    owner: dict[str, object],
    *,
    principal_id: str,
    token: str,
    role: str,
    kind: str,
    project_id: str | None = None,
) -> dict[str, object]:
    created_at = datetime.now(UTC)
    expires_at = _future()
    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            """INSERT INTO principals (id, org_id, role, token_hash, revoked_at, created_at)
            VALUES (?, 'org_local', ?, ?, NULL, ?)""",
            (principal_id, role, _token_hash(token), created_at.isoformat().replace("+00:00", "Z")),
        )
        connection.execute(
            """INSERT INTO credentials
            (id, principal_id, kind, audience, project_id, parent_id, expires_at,
             revoked_at, created_at, created_by_principal_id)
            VALUES (?, ?, ?, 'control', ?, ?, ?, NULL, ?, ?)""",
            (
                f"credential-{principal_id}",
                principal_id,
                kind,
                project_id,
                owner["credential_id"],
                expires_at,
                created_at.isoformat().replace("+00:00", "Z"),
                owner["id"],
            ),
        )
    return Store(state).authenticate(token)


def test_thread_message_reply_and_pagination(tmp_path: Path) -> None:
    _, interaction, actor, project = _setup(tmp_path)
    status, thread = interaction.create_thread(actor, "thread", project, "Inbox")
    assert status == 201
    status, message = interaction.send_message(
        actor, "message", thread["id"], "question", "What changed?"
    )
    assert status == 201 and message["delivery_status"] == "queued"
    status, reply = interaction.send_message(
        actor, "reply", thread["id"], "status", "Nothing.", message["id"]
    )
    assert status == 201
    assert [m["id"] for m in interaction.list_messages(actor, thread["id"], limit=1)] == [
        message["id"]
    ]
    assert interaction.list_messages(actor, thread["id"], offset=1, limit=1)[0]["id"] == reply["id"]


def test_decision_dedup_expiry_and_capacity(tmp_path: Path) -> None:
    state, interaction, actor, project = _setup(tmp_path)
    expires_at = _future()
    first = interaction.request_decision(actor, "d1", project, "direction", {"x": 1}, expires_at)
    duplicate = interaction.request_decision(
        actor, "d2", project, "direction", {"x": 1}, expires_at
    )
    assert first[0] == 201 and duplicate == (200, first[1])
    for n in range(49):
        assert (
            interaction.request_decision(
                actor, f"fill-{n}", project, "direction", {"x": n + 2}, _future()
            )[0]
            == 201
        )
    with pytest.raises(Conflict, match="full"):
        interaction.request_decision(actor, "full", project, "direction", {"x": 999}, _future())
    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            "UPDATE decision_requests SET expires_at = '2020-01-01T00:00:00Z' WHERE id = ?",
            (first[1]["id"],),
        )
    with pytest.raises(Conflict, match="expired"):
        interaction.resolve_decision(actor, "resolve-expired", first[1]["id"], 1, "approve")
    expired = interaction.expire_decisions(actor, "expire", project)
    assert expired == (200, {"expired": [first[1]["id"]]})
    assert (
        interaction.request_decision(
            actor, "replacement", project, "direction", {"x": 1000}, _future()
        )[0]
        == 201
    )


def test_reviewers_root_override_and_valid_rejection(tmp_path: Path) -> None:
    state, interaction, owner, project = _setup(tmp_path)
    reviewer = _principal(
        state,
        owner,
        principal_id="reviewer",
        token="reviewer-token",
        role="human",
        kind="human_session",
    )
    wrong_reviewer = _principal(
        state,
        owner,
        principal_id="wrong-reviewer",
        token="wrong-reviewer-token",
        role="human",
        kind="human_session",
    )
    _, decision = interaction.request_decision(
        owner,
        "review-root",
        project,
        "direction",
        {"x": 1},
        _future(),
        reviewer_principal_id=reviewer["id"],
    )
    with pytest.raises(Forbidden, match="designated"):
        interaction.resolve_decision(wrong_reviewer, "wrong", decision["id"], 1, "approve")
    assert (
        interaction.resolve_decision(owner, "root", decision["id"], 1, "approve")[1]["status"]
        == "approved"
    )
    _, rejected = interaction.request_decision(
        owner,
        "review-reject",
        project,
        "direction",
        {"x": 2},
        _future(),
        reviewer_principal_id=reviewer["id"],
    )
    result = interaction.resolve_decision(reviewer, "reject", rejected["id"], 1, "reject")
    assert result[0] == 200 and result[1]["status"] == "rejected"


def test_stale_task_basis_and_scoped_agent_cross_project_denial(tmp_path: Path) -> None:
    state, interaction, owner, first_project = _setup(tmp_path)
    second_project = "project-other"
    _project(state, second_project)
    store = Store(state)
    _, task = store.create_task(
        owner,
        first_project,
        "task-key",
        "task-digest",
        {
            "title": "Task",
            "purpose": "A decision must retain a current task basis.",
            "budget_cents": 0,
            "max_duration_seconds": 60,
        },
    )
    _, decision = interaction.request_decision(
        owner,
        "task-decision",
        first_project,
        "direction",
        {"x": 1},
        _future(),
        task_id=task["id"],
        expected_task_revision=1,
    )
    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute("UPDATE tasks SET revision = 2 WHERE id = ?", (task["id"],))
    with pytest.raises(Conflict, match="basis"):
        interaction.resolve_decision(owner, "task-stale", decision["id"], 1, "approve")

    _, foreign_thread = interaction.create_thread(
        owner, "foreign-thread", second_project, "Foreign"
    )
    _, foreign_decision = interaction.request_decision(
        owner, "foreign-decision", second_project, "direction", {"x": 2}, _future()
    )
    agent = _principal(
        state,
        owner,
        principal_id="scoped-agent",
        token="scoped-agent-token",
        role="agent",
        kind="agent",
        project_id=first_project,
    )
    with pytest.raises(NotFound):
        interaction.list_threads(agent, second_project)
    with pytest.raises(NotFound):
        interaction.list_messages(agent, foreign_thread["id"])
    with pytest.raises(NotFound):
        interaction.list_decisions(agent, second_project)
    with pytest.raises(NotFound):
        interaction.create_thread(agent, "agent-thread", second_project, "Denied")
    with pytest.raises(NotFound):
        interaction.send_message(agent, "agent-message", foreign_thread["id"], "question", "Denied")
    with pytest.raises(NotFound):
        interaction.request_decision(
            agent,
            "agent-decision",
            second_project,
            "direction",
            {"x": 3},
            _future(),
            reviewer_principal_id=owner["id"],
        )
    with pytest.raises(NotFound):
        interaction.expire_decisions(agent, "agent-expire", second_project)
    with pytest.raises(NotFound):
        interaction.resolve_decision(agent, "agent-resolve", foreign_decision["id"], 1, "approve")


def test_cached_revocation_and_event_failure_fail_closed_and_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, interaction, owner, project = _setup(tmp_path)
    _, thread = interaction.create_thread(owner, "thread", project, "Inbox")
    interaction.send_message(owner, "message", thread["id"], "question", "Question")
    interaction.request_decision(owner, "decision", project, "direction", {"x": 1}, _future())
    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            "UPDATE principals SET revoked_at = ? WHERE id = ?",
            ("2020-01-01T00:00:00Z", owner["id"]),
        )
    for read in (
        lambda: interaction.list_threads(owner, project),
        lambda: interaction.list_messages(owner, thread["id"]),
        lambda: interaction.list_decisions(owner, project),
    ):
        with pytest.raises(Forbidden):
            read()

    rollback_tmp = tmp_path / "rollback"
    rollback_tmp.mkdir()
    state, interaction, owner, project = _setup(rollback_tmp)

    def fail_event(*_: object) -> None:
        raise sqlite3.OperationalError("injected event failure")

    with monkeypatch.context() as patch:
        patch.setattr(Store, "_event", staticmethod(fail_event))
        with pytest.raises(sqlite3.OperationalError, match="injected"):
            interaction.create_thread(owner, "rollback", project, "Rollback")
    with _connect(state / "tessera.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM interaction_threads").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM events").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM idempotency").fetchone()[0] == 0
    assert interaction.create_thread(owner, "rollback", project, "Rollback")[0] == 201


def test_scope_rejects_nan(tmp_path: Path) -> None:
    _, interaction, owner, project = _setup(tmp_path)
    with pytest.raises(ValueError, match="strict JSON"):
        interaction.request_decision(
            owner, "nan", project, "direction", {"value": float("nan")}, _future()
        )
