"""Deterministic control event polling, replay, and tenant-fence coverage."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tessera_control.api import create_app
from tessera_control.store import Store, _connect, canonical_digest, initialize_state


def _headers(state: Path, key: str | None = None, token: str | None = None) -> dict[str, str]:
    if token is None:
        token = (state / "owner.header").read_text().strip().split("Bearer ", 1)[1]
    headers = {"Authorization": f"Bearer {token}"}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def _create_project(client: TestClient, state: Path, name: str) -> dict[str, object]:
    response = client.post(
        "/projects", json={"name": name}, headers=_headers(state, f"project-{name}")
    )
    assert response.status_code == 201, response.text
    return response.json()


def _task_body(title: str) -> dict[str, object]:
    return {
        "title": title,
        "purpose": "A deterministic control-plane event fixture.",
        "budget_cents": 0,
        "max_duration_seconds": 60,
    }


def test_labbook_cursor_pagination_replay_and_concurrent_consumers_are_project_scoped(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    initialize_state(state)
    client = TestClient(create_app(state))
    project = _create_project(client, state, "Event stream")

    first_body = _task_body("first")
    first = client.post(
        f"/projects/{project['id']}/tasks", json=first_body, headers=_headers(state, "lost-reply")
    )
    replay = client.post(
        f"/projects/{project['id']}/tasks", json=first_body, headers=_headers(state, "lost-reply")
    )
    conflicting_retry = client.post(
        f"/projects/{project['id']}/tasks",
        json=_task_body("different request"),
        headers=_headers(state, "lost-reply"),
    )
    assert first.status_code == replay.status_code == 201
    assert first.json() == replay.json()
    assert conflicting_retry.status_code == 409
    assert (
        client.post(
            f"/projects/{project['id']}/tasks",
            json=_task_body("second"),
            headers=_headers(state, "second-task"),
        ).status_code
        == 201
    )

    all_events = client.get(
        f"/projects/{project['id']}/labbook?after=0&limit=100", headers=_headers(state)
    )
    assert all_events.status_code == 200
    events = all_events.json()["events"]
    cursors = [event["cursor"] for event in events]
    assert cursors == sorted(cursors) and len(cursors) == len(set(cursors)) == 3
    assert [event["event_type"] for event in events] == [
        "project.created",
        "task.created",
        "task.created",
    ]
    assert [event["payload"]["title"] for event in events[1:]] == ["first", "second"]

    # `after` is exclusive, so reconnecting from the retained cursor neither
    # duplicates a consumed event nor skips the next durable event.
    first_page = client.get(
        f"/projects/{project['id']}/labbook?after=0&limit=1", headers=_headers(state)
    ).json()["events"]
    second_page = client.get(
        f"/projects/{project['id']}/labbook?after={first_page[-1]['cursor']}&limit=1",
        headers=_headers(state),
    ).json()["events"]
    replay_page = client.get(
        f"/projects/{project['id']}/labbook?after={first_page[-1]['cursor']}&limit=100",
        headers=_headers(state),
    ).json()["events"]
    assert first_page + replay_page == events
    assert second_page == [events[1]]

    def consume() -> list[dict[str, object]]:
        with TestClient(create_app(state)) as consumer:
            response = consumer.get(
                f"/projects/{project['id']}/labbook?after=0&limit=100", headers=_headers(state)
            )
            assert response.status_code == 200
            return response.json()["events"]

    with ThreadPoolExecutor(max_workers=2) as executor:
        left, right = list(executor.map(lambda _: consume(), range(2)))
    assert left == right == events

    other = _create_project(client, state, "Other project")
    token = "a" * 43
    credential = client.post(
        "/credentials",
        json={
            "token": token,
            "kind": "agent",
            "audience": "control",
            "project_id": project["id"],
            "expires_at": (datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
        },
        headers=_headers(state, "project-agent"),
    )
    assert credential.status_code == 201
    agent_headers = _headers(state, token=token)
    assert (
        client.get(
            f"/projects/{project['id']}/labbook?after=0&limit=100", headers=agent_headers
        ).json()["events"]
        == events
    )
    assert (
        client.get(
            f"/projects/{other['id']}/labbook?after=0&limit=100", headers=agent_headers
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/projects/{other['id']}/tasks",
            json=_task_body("cross-project command"),
            headers=agent_headers | {"Idempotency-Key": "cross-project-command"},
        ).status_code
        == 404
    )


def test_event_state_and_idempotency_commit_or_roll_back_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    initialize_state(state)
    client = TestClient(create_app(state))
    project = _create_project(client, state, "Atomic events")
    store = Store(state)
    owner_token = (state / "owner.header").read_text().strip().split("Bearer ", 1)[1]
    owner = store.authenticate(owner_token)
    body = _task_body("must roll back")

    def fail_event(*_: object) -> None:
        raise sqlite3.OperationalError("injected event write failure")

    with monkeypatch.context() as patch:
        patch.setattr(Store, "_event", staticmethod(fail_event))
        with pytest.raises(sqlite3.OperationalError, match="injected event"):
            store.create_task(
                owner, str(project["id"]), "atomic-task", canonical_digest(body), body
            )

    with _connect(state / "tessera.sqlite3") as connection:
        task_count = connection.execute(
            "SELECT count(*) FROM tasks WHERE title='must roll back'"
        ).fetchone()[0]
        assert task_count == 0
        assert (
            connection.execute(
                "SELECT count(*) FROM events WHERE event_type='task.created'"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM idempotency WHERE idempotency_key='atomic-task'"
            ).fetchone()[0]
            == 0
        )
