from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tessera_agent import (
    AgentCredential,
    AstraAgentFacade,
    ContextReference,
    ProviderDisabledCoordinator,
    ProviderDisabledCoordinatorConsumer,
    StaleContextError,
)
from tessera_control.api import create_app
from tessera_control.migrations import LATEST_SCHEMA_VERSION
from tessera_control.recovery import backup_state, restore_state
from tessera_control.store import _connect, _token_hash, initialize_state, migrate_state


def _headers(state: Path, key: str, token: str | None = None) -> dict[str, str]:
    if token is None:
        _, token = (state / "owner.header").read_text().strip().split("Bearer ", 1)
    return {"Authorization": f"Bearer {token}", "Idempotency-Key": key}


def _project(client: TestClient, state: Path, name: str) -> dict[str, object]:
    response = client.post(
        "/projects", headers=_headers(state, f"project-{name}"), json={"name": name}
    )
    assert response.status_code == 201
    return response.json()


def _agent(
    client: TestClient, state: Path, project_id: str, token: str = "a" * 43
) -> tuple[str, AstraAgentFacade]:
    response = client.post(
        "/credentials",
        headers=_headers(state, f"astra-agent-{token[0]}"),
        json={
            "token": token,
            "kind": "agent",
            "audience": "control",
            "project_id": project_id,
            "expires_at": (datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
        },
    )
    assert response.status_code == 201
    return token, AstraAgentFacade(client, AgentCredential(token=token, project_id=project_id))


def _task(client: TestClient, state: Path, project_id: str) -> dict[str, object]:
    response = client.post(
        f"/projects/{project_id}/tasks",
        headers=_headers(state, "astra-task"),
        json={
            "title": "Astra fixture",
            "purpose": "A provider-disabled coordinator fixture.",
            "budget_cents": 0,
            "max_duration_seconds": 60,
        },
    )
    assert response.status_code == 201
    return response.json()


def _request(
    client: TestClient, state: Path, project_id: str, revision: int, task: dict[str, object]
) -> dict[str, object]:
    response = client.post(
        f"/astra/projects/{project_id}/turns",
        headers=_headers(state, f"request-{revision}"),
        json={
            "expected_conversation_revision": revision,
            "prompt": "Give a bounded status without taking action.",
            "selected_context": [
                {"kind": "task", "record_id": task["id"], "expected_revision": task["revision"]}
            ],
        },
    )
    assert response.status_code == 201
    return response.json()


def _design(task: dict[str, object]) -> dict[str, object]:
    return {
        "goal": "Compare a recorded baseline with one bounded intervention.",
        "success_criterion": "A human compares the registered measurements.",
        "protocol": (
            "Register one baseline and one intervention before any separate execution review."
        ),
        "variants": [
            {
                "kind": "baseline",
                "name": "baseline",
                "method_digest": "a" * 64,
                "input_digest": "b" * 64,
                "metadata": {"source": "deterministic-coordinator"},
                "execution_task_id": task["id"],
                "execution_plan_digest": task["plan_digest"],
            },
            {
                "kind": "intervention",
                "name": "intervention",
                "method_digest": "c" * 64,
                "input_digest": "d" * 64,
                "metadata": {"source": "deterministic-coordinator"},
                "execution_task_id": task["id"],
                "execution_plan_digest": task["plan_digest"],
            },
        ],
    }


def _complete_design_proposal(
    client: TestClient,
    state: Path,
    project_id: str,
    task: dict[str, object],
    design: dict[str, object] | None = None,
    token: str = "a" * 43,
) -> tuple[str, dict[str, object]]:
    token, _ = _agent(client, state, project_id, token)
    requested = _request(client, state, project_id, 0, task)
    claimed = client.post(
        f"/astra/turns/{requested['turn']['id']}/claim",
        headers=_headers(state, "design-claim", token),
        json={"expected_revision": requested["turn"]["revision"]},
    )
    assert claimed.status_code == 200
    completed = client.post(
        f"/astra/turns/{requested['turn']['id']}/complete",
        headers=_headers(state, "design-complete", token),
        json={
            "expected_revision": claimed.json()["revision"],
            "status": ["A deterministic candidate is ready for human registration."],
            "recap": "No authority was created.",
            "proposal": "A human may register this design after reviewing its current context.",
            "proposal_type": "human_review_needed",
            "design": _design(task) if design is None else design,
            "provider_disabled": True,
            "unsupported_actions": [
                "approval",
                "grant",
                "dispatch",
                "tool",
                "shell",
                "skill_loading",
            ],
        },
    )
    assert completed.status_code == 200
    return token, completed.json()["proposal"]


def test_provider_disabled_turn_is_idempotent_and_has_no_authority(tmp_path: Path) -> None:
    state = tmp_path / "state"
    initialize_state(state)
    client = TestClient(create_app(state))
    project = _project(client, state, "Astra")
    task = _task(client, state, str(project["id"]))
    token, facade = _agent(client, state, str(project["id"]))
    requested = _request(client, state, str(project["id"]), 0, task)
    turn = requested["turn"]
    assert _request(client, state, str(project["id"]), 0, task) == requested

    claim = client.post(
        f"/astra/turns/{turn['id']}/claim",
        headers=_headers(state, "claim", token),
        json={"expected_revision": turn["revision"]},
    )
    assert claim.status_code == 200
    output = ProviderDisabledCoordinator(facade).assemble(
        "Give a bounded status without taking action.",
        [
            ContextReference(
                kind="task", record_id=str(task["id"]), expected_revision=int(task["revision"])
            )
        ],
    )
    completion_payload = {
        "expected_revision": claim.json()["revision"],
        **output.model_dump(mode="json"),
    }
    completed = client.post(
        f"/astra/turns/{turn['id']}/complete",
        headers=_headers(state, "complete", token),
        json=completion_payload,
    )
    assert completed.status_code == 200
    replay = client.post(
        f"/astra/turns/{turn['id']}/complete",
        headers=_headers(state, "complete", token),
        json=completion_payload,
    )
    assert replay.json() == completed.json()
    proposal = completed.json()["proposal"]
    assert proposal["provider_disabled"] is True
    assert proposal["authority_effect"] == "none"
    assert proposal["execution_authorized"] is False
    assert "approval" in output.unsupported_actions
    assert ProviderDisabledCoordinator(facade).provider_enabled is False
    with _connect(state / "tessera.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM execution_grants").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM external_operations").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM astra_proposals").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("UPDATE astra_proposals SET body='mutated'")


def test_ordinary_consumer_crash_before_and_after_claim_is_resumable_without_effects(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    initialize_state(state)
    client = TestClient(create_app(state))
    project = _project(client, state, "Ordinary consumer")
    task = _task(client, state, str(project["id"]))
    token, facade = _agent(client, state, str(project["id"]))

    # A process that dies before the claim has no control-plane effect.
    assert (
        client.post(
            "/astra/turns/next-claim", headers=_headers(state, "crash-before", token), json={}
        ).json()["turn"]
        is None
    )
    requested = _request(client, state, str(project["id"]), 0, task)
    # A process that dies after its durable claim is resumed by the same scoped
    # consumer; the resulting deterministic completion is still exactly one row.
    claimed = client.post(
        "/astra/turns/next-claim", headers=_headers(state, "crash-after", token), json={}
    )
    assert claimed.status_code == 200
    assert claimed.json()["turn"]["id"] == requested["turn"]["id"]
    result = ProviderDisabledCoordinatorConsumer(client, token, facade).run_once()
    assert result["turn"]["state"] == "completed"
    with _connect(state / "tessera.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM astra_proposals").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM execution_grants").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM external_operations").fetchone()[0] == 0


def test_ordinary_consumer_completion_replay_and_stale_claim_reclaim(tmp_path: Path) -> None:
    state = tmp_path / "state"
    initialize_state(state)
    client = TestClient(create_app(state))
    project = _project(client, state, "Ordinary replay")
    task = _task(client, state, str(project["id"]))
    first_token, first_facade = _agent(client, state, str(project["id"]))
    second_token, _ = _agent(client, state, str(project["id"]), "b" * 43)
    requested = _request(client, state, str(project["id"]), 0, task)
    first = client.post(
        "/astra/turns/next-claim", headers=_headers(state, "first-claim", first_token), json={}
    ).json()
    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            "UPDATE astra_coordinator_turns SET claim_expires_at=? WHERE id=?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), requested["turn"]["id"]),
        )
    reclaimed = client.post(
        "/astra/turns/next-claim", headers=_headers(state, "second-claim", second_token), json={}
    )
    assert reclaimed.status_code == 200
    turn = reclaimed.json()["turn"]
    assert turn["id"] == first["turn"]["id"]
    assert turn["claim_attempts"] == 2
    output = ProviderDisabledCoordinator(first_facade).assemble(
        "Give a bounded status without taking action.",
        [
            ContextReference(
                kind="task",
                record_id=str(task["id"]),
                expected_revision=int(task["revision"]),
            )
        ],
    )
    body = {"expected_revision": turn["revision"], **output.model_dump(mode="json")}
    path = f"/astra/turns/{turn['id']}/complete"
    completed = client.post(
        path, headers=_headers(state, "reclaim-complete", second_token), json=body
    )
    replay = client.post(path, headers=_headers(state, "reclaim-complete", second_token), json=body)
    assert completed.status_code == 200
    assert replay.json() == completed.json()


def test_ordinary_consumer_reports_unavailable_then_idle_when_no_work(tmp_path: Path) -> None:
    state = tmp_path / "state"
    initialize_state(state)
    client = TestClient(create_app(state))
    project = _project(client, state, "No consumer work")
    token, facade = _agent(client, state, str(project["id"]))
    before = client.get(
        f"/astra/projects/{project['id']}/consumer-status", headers=_headers(state, "before")
    )
    assert before.json()["availability"] == "unavailable"
    result = ProviderDisabledCoordinatorConsumer(client, token, facade).run_once()
    assert result == {"availability": "idle", "provider_disabled": True, "provider_enabled": False}
    after = client.get(
        f"/astra/projects/{project['id']}/consumer-status", headers=_headers(state, "after")
    )
    assert after.json()["availability"] == "available"


def test_consumer_status_never_treats_revoked_or_expired_heartbeat_as_available(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    initialize_state(state)
    client = TestClient(create_app(state))
    project = _project(client, state, "Consumer credential status")
    token, facade = _agent(client, state, str(project["id"]))
    ProviderDisabledCoordinatorConsumer(client, token, facade).run_once()

    available = client.get(
        f"/astra/projects/{project['id']}/consumer-status", headers=_headers(state, "available")
    )
    assert available.json()["credential_status"] == "active"
    assert available.json()["freshness"] == "fresh"
    assert available.json()["availability"] == "available"

    with _connect(state / "tessera.sqlite3") as connection:
        credential_id = connection.execute(
            """SELECT c.id FROM credentials c JOIN principals p ON p.id=c.principal_id
            WHERE p.token_hash=?""",
            (_token_hash(token),),
        ).fetchone()[0]
    revoked = client.post(
        f"/credentials/{credential_id}/revoke", headers=_headers(state, "revoke-consumer")
    )
    assert revoked.status_code == 200
    status = client.get(
        f"/astra/projects/{project['id']}/consumer-status", headers=_headers(state, "revoked")
    ).json()
    assert status["availability"] == "unavailable"
    assert status["health"] == status["credential_status"] == "revoked"

    # A different consumer credential can expire after its heartbeat. The old
    # durable observation stays inspectable but cannot resurrect availability.
    second, second_facade = _agent(client, state, str(project["id"]), "b" * 43)
    ProviderDisabledCoordinatorConsumer(client, second, second_facade).run_once()
    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            """UPDATE credentials SET expires_at=? WHERE principal_id=(
            SELECT id FROM principals WHERE token_hash=?)""",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), _token_hash(second)),
        )
    expired = client.get(
        f"/astra/projects/{project['id']}/consumer-status", headers=_headers(state, "expired")
    ).json()
    assert expired["availability"] == "unavailable"
    assert expired["health"] == expired["credential_status"] == "expired"


def test_tenant_and_revision_fences_reject_stale_context(tmp_path: Path) -> None:
    state = tmp_path / "state"
    initialize_state(state)
    client = TestClient(create_app(state))
    project = _project(client, state, "Astra")
    foreign = _project(client, state, "Foreign")
    task = _task(client, state, str(project["id"]))
    token, facade = _agent(client, state, str(project["id"]))
    first = _request(client, state, str(project["id"]), 0, task)
    second = _request(client, state, str(project["id"]), 1, task)
    stale_claim = client.post(
        f"/astra/turns/{first['turn']['id']}/claim",
        headers=_headers(state, "stale-claim", token),
        json={"expected_revision": 1},
    )
    assert stale_claim.status_code == 409
    assert "stale" in stale_claim.json()["detail"]
    foreign_read = client.get(
        f"/astra/projects/{foreign['id']}/conversation", headers=_headers(state, "read", token)
    )
    assert foreign_read.status_code == 404

    revised = client.put(
        f"/tasks/{task['id']}/plan",
        headers=_headers(state, "revise"),
        json={
            "expected_revision": task["revision"],
            "title": task["title"],
            "purpose": task["purpose"],
            "budget_cents": 0,
            "max_duration_seconds": 60,
            "execution_spec": {
                "argv": ["python", "-c", "print('fixture')"],
                "input_snapshot_digest": "1" * 64,
                "pinned_skill_digests": [],
                "output_paths": ["result.txt"],
                "profile": "local-cpu",
                "network_enabled": False,
                "max_output_bytes": 1024,
            },
        },
    )
    assert revised.status_code == 200
    with pytest.raises(StaleContextError):
        ProviderDisabledCoordinator(facade).assemble(
            "Check revision.",
            [ContextReference(kind="task", record_id=str(task["id"]), expected_revision=1)],
        )
    stale_request = client.post(
        f"/astra/projects/{project['id']}/turns",
        headers=_headers(state, "stale-request"),
        json={
            "expected_conversation_revision": 2,
            "prompt": "Use stale selected context.",
            "selected_context": [{"kind": "task", "record_id": task["id"], "expected_revision": 1}],
        },
    )
    assert stale_request.status_code == 409
    assert second["turn"]["state"] == "requested"

    claim = client.post(
        f"/astra/turns/{second['turn']['id']}/claim",
        headers=_headers(state, "claim-current", token),
        json={"expected_revision": 1},
    )
    assert claim.status_code == 200
    failed = client.post(
        f"/astra/turns/{second['turn']['id']}/fail",
        headers=_headers(state, "fail-current", token),
        json={"expected_revision": claim.json()["revision"], "error_code": "stale_context"},
    )
    assert failed.status_code == 200
    assert failed.json()["state"] == "failed"
    cancelled = _request(client, state, str(project["id"]), 2, revised.json())
    cancelled_response = client.post(
        f"/astra/turns/{cancelled['turn']['id']}/cancel",
        headers=_headers(state, "cancel", token=None),
        json={"expected_revision": 1},
    )
    assert cancelled_response.status_code == 200
    assert cancelled_response.json()["state"] == "cancelled"


def test_human_registers_one_immutable_coordinator_design_without_execution_authority(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    initialize_state(state)
    client = TestClient(create_app(state))
    project = _project(client, state, "Astra design")
    task = _task(client, state, str(project["id"]))
    _agent_token, proposal = _complete_design_proposal(client, state, str(project["id"]), task)

    body = {"expected_context_digest": proposal["context_digest"]}
    path = f"/astra/proposals/{proposal['id']}/register-design"
    registered = client.post(path, headers=_headers(state, "register-design"), json=body)
    assert registered.status_code == 201
    replay = client.post(path, headers=_headers(state, "register-design"), json=body)
    assert replay.json() == registered.json()
    registration = registered.json()["registration"]
    assert registration["proposal_id"] == proposal["id"]
    assert registration["design_revision"] == 1
    assert registered.json()["execution_authorized"] is False
    assert {
        variant["kind"] for variant in registered.json()["experiment"]["design"]["variants"]
    } == {
        "baseline",
        "intervention",
    }
    proposals = client.get(
        f"/astra/projects/{project['id']}/proposals", headers=_headers(state, "proposal-history")
    )
    assert proposals.status_code == 200
    history = proposals.json()["proposals"]
    assert len(history) == 1
    assert history[0]["body"] == proposal["body"]
    assert history[0]["registration"]["experiment_id"] == registration["experiment_id"]
    duplicate = client.post(path, headers=_headers(state, "duplicate-design"), json=body)
    assert duplicate.status_code == 409
    with _connect(state / "tessera.sqlite3") as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM astra_proposal_design_registrations"
            ).fetchone()[0]
            == 1
        )
        assert connection.execute("SELECT count(*) FROM research_experiments").fetchone()[0] == 1
        assert (
            connection.execute("SELECT count(*) FROM research_experiment_designs").fetchone()[0]
            == 1
        )
        assert (
            connection.execute("SELECT count(*) FROM research_experiment_variants").fetchone()[0]
            == 2
        )
        for table in (
            "task_approvals",
            "execution_grants",
            "execution_leases",
            "external_operations",
            "research_conclusions",
        ):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_backup_restore_preserves_coordinator_proposal_and_registration_records(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    initialize_state(state)
    client = TestClient(create_app(state))
    project = _project(client, state, "Recovered Astra design")
    task = _task(client, state, str(project["id"]))
    agent_token, proposal = _complete_design_proposal(client, state, str(project["id"]), task)
    registered = client.post(
        f"/astra/proposals/{proposal['id']}/register-design",
        headers=_headers(state, "register-recovered-design"),
        json={"expected_context_digest": proposal["context_digest"]},
    )
    assert registered.status_code == 201
    turn_id = proposal["turn_id"]
    tables = (
        "astra_project_conversations",
        "astra_conversation_messages",
        "astra_selected_context_snapshots",
        "astra_coordinator_turns",
        "astra_proposals",
        "astra_proposal_design_registrations",
    )

    def astra_rows(path: Path) -> dict[str, list[tuple[object, ...]]]:
        with _connect(path / "tessera.sqlite3") as connection:
            return {
                table: connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
                for table in tables
            }

    before = astra_rows(state)
    before_history = client.get(
        f"/astra/projects/{project['id']}/proposals",
        headers=_headers(state, "proposal-before-backup"),
    ).json()
    backup = tmp_path / "backup"
    assert backup_state(state, backup)["schema_version"] == LATEST_SCHEMA_VERSION
    restored = tmp_path / "restored"
    restore_state(backup, restored)

    assert astra_rows(restored) == before
    restored_client = TestClient(create_app(restored))
    assert (
        restored_client.get(
            f"/astra/projects/{project['id']}/proposals",
            headers=_headers(restored, "proposal-after-restore"),
        ).json()
        == before_history
    )
    assert (
        restored_client.get(
            f"/astra/turns/{turn_id}", headers=_headers(restored, "turn-after-restore")
        ).status_code
        == 200
    )
    assert (
        restored_client.get(
            f"/astra/turns/{turn_id}",
            headers={"Authorization": f"Bearer {agent_token}"},
        ).status_code
        == 401
    )
    held_registration = restored_client.post(
        f"/astra/proposals/{proposal['id']}/register-design",
        headers=_headers(restored, "held-registration"),
        json={"expected_context_digest": proposal["context_digest"]},
    )
    assert held_registration.status_code == 403
    with _connect(restored / "tessera.sqlite3") as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM astra_proposal_design_registrations"
            ).fetchone()[0]
            == 1
        )
        for table in (
            "task_approvals",
            "execution_grants",
            "execution_leases",
            "external_operations",
            "research_conclusions",
        ):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_design_registration_rejects_stale_or_foreign_execution_plans(tmp_path: Path) -> None:
    state = tmp_path / "state"
    initialize_state(state)
    client = TestClient(create_app(state))
    project = _project(client, state, "Astra stale design")
    task = _task(client, state, str(project["id"]))
    _agent_token, proposal = _complete_design_proposal(client, state, str(project["id"]), task)
    revised = client.put(
        f"/tasks/{task['id']}/plan",
        headers=_headers(state, "stale-design-plan"),
        json={
            "expected_revision": task["revision"],
            "title": task["title"],
            "purpose": task["purpose"],
            "budget_cents": 0,
            "max_duration_seconds": 60,
            "execution_spec": {
                "argv": ["python", "-c", "print('fixture')"],
                "input_snapshot_digest": "1" * 64,
                "pinned_skill_digests": [],
                "output_paths": ["result.txt"],
                "profile": "local-cpu",
                "network_enabled": False,
                "max_output_bytes": 1024,
            },
        },
    )
    assert revised.status_code == 200
    stale = client.post(
        f"/astra/proposals/{proposal['id']}/register-design",
        headers=_headers(state, "stale-design-register"),
        json={"expected_context_digest": proposal["context_digest"]},
    )
    assert stale.status_code == 409

    foreign = _project(client, state, "Foreign design task")
    foreign_task = _task(client, state, str(foreign["id"]))
    second = _project(client, state, "Foreign plan candidate")
    second_task = _task(client, state, str(second["id"]))
    _agent_token, foreign_proposal = _complete_design_proposal(
        client, state, str(second["id"]), second_task, _design(foreign_task), "b" * 43
    )
    # A foreign task plan cannot be smuggled through a syntactically valid,
    # otherwise immutable proposal from this project.
    foreign_plan = client.post(
        f"/astra/proposals/{foreign_proposal['id']}/register-design",
        headers=_headers(state, "foreign-design-register"),
        json={"expected_context_digest": foreign_proposal["context_digest"]},
    )
    assert foreign_plan.status_code == 404


def test_v41_migration_adds_no_astra_records_or_authority(tmp_path: Path) -> None:
    state = tmp_path / "state"
    initialize_state(state)
    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute("DROP TABLE astra_proposal_design_registrations")
        for table in (
            "astra_proposals",
            "astra_coordinator_turns",
            "astra_selected_context_snapshots",
            "astra_conversation_messages",
            "astra_project_conversations",
        ):
            connection.execute(f"DROP TABLE {table}")
        from migration_fixture_helpers import strip_empty_runtime_v55

        strip_empty_runtime_v55(connection)
        connection.execute("UPDATE schema_meta SET schema_version=39")
    assert migrate_state(state) == LATEST_SCHEMA_VERSION
    with _connect(state / "tessera.sqlite3") as connection:
        conversation_count = connection.execute(
            "SELECT count(*) FROM astra_project_conversations"
        ).fetchone()[0]
        assert conversation_count == 0
        assert connection.execute("SELECT count(*) FROM astra_coordinator_turns").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM astra_proposals").fetchone()[0] == 0
        for table in (
            "astra_status_discussions",
            "astra_status_discussion_bindings",
            "astra_status_tool_invocations",
            "astra_status_answers",
        ):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT count(*) FROM astra_proposal_design_registrations"
            ).fetchone()[0]
            == 0
        )
        assert connection.execute("SELECT count(*) FROM execution_grants").fetchone()[0] == 0


def test_v40_to_v41_preserves_legacy_proposal_without_inventing_promotion(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    initialize_state(state)
    client = TestClient(create_app(state))
    project = _project(client, state, "Legacy Astra proposal")
    task = _task(client, state, str(project["id"]))
    agent_token, _facade = _agent(client, state, str(project["id"]))
    requested = _request(client, state, str(project["id"]), 0, task)
    claimed = client.post(
        f"/astra/turns/{requested['turn']['id']}/claim",
        headers=_headers(state, "legacy-claim", agent_token),
        json={"expected_revision": 1},
    )
    completed = client.post(
        f"/astra/turns/{requested['turn']['id']}/complete",
        headers=_headers(state, "legacy-complete", agent_token),
        json={
            "expected_revision": claimed.json()["revision"],
            "status": ["A legacy non-authoritative proposal."],
            "recap": "No design or authority was recorded.",
            "proposal": "Continue human review.",
            "proposal_type": "human_review_needed",
            "provider_disabled": True,
            "unsupported_actions": [
                "approval",
                "grant",
                "dispatch",
                "tool",
                "shell",
                "skill_loading",
            ],
        },
    )
    assert completed.status_code == 200
    proposal = completed.json()["proposal"]
    with _connect(state / "tessera.sqlite3") as connection:
        legacy = connection.execute(
            "SELECT id,body,recap,status_json,created_at,created_by_principal_id "
            "FROM astra_proposals WHERE id=?",
            (proposal["id"],),
        ).fetchone()
        connection.execute("DROP TABLE astra_proposal_design_registrations")
        connection.execute("ALTER TABLE astra_proposals DROP COLUMN design_json")
        from migration_fixture_helpers import strip_empty_runtime_v55

        strip_empty_runtime_v55(connection)
        connection.execute("UPDATE schema_meta SET schema_version=40")

    assert migrate_state(state) == LATEST_SCHEMA_VERSION
    with _connect(state / "tessera.sqlite3") as connection:
        migrated = connection.execute(
            "SELECT id,body,recap,status_json,created_at,created_by_principal_id "
            "FROM astra_proposals WHERE id=?",
            (proposal["id"],),
        ).fetchone()
        assert migrated == legacy
        assert (
            connection.execute(
                "SELECT design_json FROM astra_proposals WHERE id=?", (proposal["id"],)
            ).fetchone()[0]
            is None
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM astra_proposal_design_registrations"
            ).fetchone()[0]
            == 0
        )
        for table in (
            "task_approvals",
            "execution_grants",
            "execution_leases",
            "external_operations",
        ):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0

    migrated_client = TestClient(create_app(state))
    new_project = _project(migrated_client, state, "Post-migration Astra proposal")
    new_task = _task(migrated_client, state, str(new_project["id"]))
    _token, design_proposal = _complete_design_proposal(
        migrated_client, state, str(new_project["id"]), new_task, token="b" * 43
    )
    registered = migrated_client.post(
        f"/astra/proposals/{design_proposal['id']}/register-design",
        headers=_headers(state, "post-migration-registration"),
        json={"expected_context_digest": design_proposal["context_digest"]},
    )
    assert registered.status_code == 201
    assert registered.json()["execution_authorized"] is False
