from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tessera_agent import AgentCredential, AstraAgentFacade, ControlPlaneError, ForbiddenToolError
from tessera_agent.facade import ToolInputError, UnknownToolError
from tessera_control.api import create_app
from tessera_control.store import initialize_state
from tessera_control.workspace import skill_manifest_digest


def _owner_headers(state_dir: Path, key: str) -> dict[str, str]:
    _, value = (state_dir / "owner.header").read_text().strip().split(": ", 1)
    return {"Authorization": value, "Idempotency-Key": key}


def _project(client: TestClient, state_dir: Path, name: str) -> dict[str, object]:
    response = client.post(
        "/projects", headers=_owner_headers(state_dir, f"project-{name}"), json={"name": name}
    )
    assert response.status_code == 201
    return response.json()


def _agent(client: TestClient, state_dir: Path, project_id: str, token: str) -> AstraAgentFacade:
    response = client.post(
        "/credentials",
        headers=_owner_headers(state_dir, f"agent-{project_id}"),
        json={
            "token": token,
            "kind": "agent",
            "audience": "control",
            "project_id": project_id,
            "expires_at": (datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
        },
    )
    assert response.status_code == 201
    return AstraAgentFacade(client, AgentCredential(token=token, project_id=project_id))


def _task(client: TestClient, state_dir: Path, project_id: str) -> dict[str, object]:
    response = client.post(
        f"/projects/{project_id}/tasks",
        headers=_owner_headers(state_dir, "task"),
        json={
            "title": "Bounded baseline",
            "purpose": "Read-only facade fixture.",
            "budget_cents": 0,
            "max_duration_seconds": 60,
        },
    )
    assert response.status_code == 201
    return response.json()


@pytest.fixture
def facade(
    tmp_path: Path,
) -> tuple[AstraAgentFacade, TestClient, Path, dict[str, object], dict[str, object]]:
    state_dir = tmp_path / "state"
    initialize_state(state_dir)
    client = TestClient(create_app(state_dir))
    project = _project(client, state_dir, "Astra")
    other_project = _project(client, state_dir, "Other")
    agent = _agent(client, state_dir, str(project["id"]), "a" * 43)
    return agent, client, state_dir, project, other_project


def test_reads_task_and_current_plan_through_scoped_agent_api(
    facade: tuple[AstraAgentFacade, TestClient, Path, dict[str, object], dict[str, object]],
) -> None:
    agent, client, state_dir, project, _ = facade
    task = _task(client, state_dir, str(project["id"]))
    status = agent.call("read_task", {"identifier": task["id"]})
    assert status.id == task["id"]
    assert status.project_id == project["id"]

    plan = {
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
    }
    response = client.put(
        f"/tasks/{task['id']}/plan", headers=_owner_headers(state_dir, "plan"), json=plan
    )
    assert response.status_code == 200
    read_plan = agent.call("read_plan", {"identifier": task["id"]})
    assert read_plan.plan_digest == response.json()["plan_digest"]
    assert read_plan.execution_spec is not None
    assert read_plan.execution_spec.network_enabled is False


def test_rejects_unknown_consequential_and_cross_project_reads(
    facade: tuple[AstraAgentFacade, TestClient, Path, dict[str, object], dict[str, object]],
) -> None:
    agent, client, state_dir, _, other_project = facade
    foreign_task = _task(client, state_dir, str(other_project["id"]))
    with pytest.raises(ControlPlaneError, match="404"):
        agent.read_task(str(foreign_task["id"]))
    with pytest.raises(UnknownToolError):
        agent.call("arbitrary_route", {})
    with pytest.raises(ForbiddenToolError):
        agent.call("approve", {})
    with pytest.raises(ForbiddenToolError):
        agent.call("load_skill", {})
    with pytest.raises(ToolInputError):
        agent.call("read_task", {"identifier": "task", "route": "/credentials"})
    assert agent.provider_enabled is False
    assert not hasattr(agent, "request")


def test_skill_response_is_metadata_only_and_never_loaded(
    facade: tuple[AstraAgentFacade, TestClient, Path, dict[str, object], dict[str, object]],
) -> None:
    agent, client, state_dir, project, _ = facade
    digest = skill_manifest_digest((("SKILL.md", "3" * 64, 1),))
    skill = {
        "name": "astra/read-only",
        "description": "Metadata-only fixture.",
        "digest": digest,
        "manifest": [{"relative_path": "SKILL.md", "sha256": "3" * 64, "size": 1}],
        "provenance": "imported_unverified",
        "classification": "unclassified",
    }
    response = client.post(
        f"/projects/{project['id']}/skills",
        headers=_owner_headers(state_dir, "skill"),
        json=skill,
    )
    assert response.status_code == 201
    metadata = agent.call("read_skill_metadata", {"identifier": response.json()["id"]})
    assert metadata.metadata_only is True
    assert metadata.materialized is False
    assert metadata.loaded is False
    assert metadata.runtime_skill_loading == "disabled"
    assert "manifest" not in metadata.model_dump()


def test_reads_thread_context_and_experiment_status_over_authenticated_api(
    facade: tuple[AstraAgentFacade, TestClient, Path, dict[str, object], dict[str, object]],
) -> None:
    agent, client, state_dir, project, _ = facade
    thread_response = client.post(
        "/interaction/threads",
        headers=_owner_headers(state_dir, "thread"),
        json={"project_id": project["id"], "title": "Bounded context"},
    )
    assert thread_response.status_code == 201
    thread = thread_response.json()
    message_response = client.post(
        "/interaction/messages",
        headers=_owner_headers(state_dir, "message"),
        json={"thread_id": thread["id"], "intent": "status", "body": "No provider was contacted."},
    )
    assert message_response.status_code == 201
    context = agent.call("read_thread_context", {"thread_id": thread["id"], "limit": 1})
    assert context.thread_id == thread["id"]
    assert [message.body for message in context.messages] == ["No provider was contacted."]

    experiment_response = client.post(
        f"/projects/{project['id']}/experiments",
        headers=_owner_headers(state_dir, "experiment"),
        json={
            "goal": "Exercise the read-only agent facade.",
            "success_criterion": "The control record remains provider-disabled.",
            "protocol": "Create only a synthetic record.",
            "variants": [
                {
                    "kind": "baseline",
                    "name": "baseline",
                    "method_digest": "4" * 64,
                    "input_digest": "5" * 64,
                    "metadata": {},
                },
                {
                    "kind": "intervention",
                    "name": "intervention",
                    "method_digest": "6" * 64,
                    "input_digest": "7" * 64,
                    "metadata": {},
                },
            ],
        },
    )
    assert experiment_response.status_code == 201
    status = agent.call("read_experiment_status", {"identifier": experiment_response.json()["id"]})
    assert status.project_id == project["id"]
    assert status.operation_count == 0
    assert status.scientific_validity == "not_assessed"


def test_reads_one_operation_status_from_the_bounded_experiment_export() -> None:
    project_id = "project_astra"
    operation_id = "a" * 32

    class Response:
        status_code = 200

        def __init__(self, body: dict[str, object]) -> None:
            self.body = body

        def json(self) -> dict[str, object]:
            return self.body

    class ReadOnlyHttpFixture:
        def get(
            self, path: str, *, headers: dict[str, str], params: dict[str, object] | None = None
        ) -> Response:
            assert headers == {"Authorization": f"Bearer {'z' * 43}"}
            if path == "/me":
                return Response(
                    {
                        "credential_id": "credential_agent",
                        "principal_id": "principal_agent",
                        "org_id": "org_local",
                        "role": "agent",
                        "kind": "agent",
                        "audience": "control",
                        "project_id": project_id,
                        "expires_at": "2026-01-01T00:00:00Z",
                        "revoked_at": None,
                        "parent_id": "credential_owner",
                    }
                )
            assert path == "/experiments/experiment_astra/export"
            assert params == {"max_bytes": 65_536}
            return Response(
                {
                    "experiment": {
                        "id": "experiment_astra",
                        "project_id": project_id,
                        "current_design_revision": 1,
                    },
                    "operations": [
                        {
                            "operation_id": operation_id,
                            "project_id": project_id,
                            "task_id": "task_astra",
                            "desired_state": "admitted",
                            "observed_state": "running",
                            "provider_id": None,
                        }
                    ],
                }
            )

    agent = AstraAgentFacade(
        ReadOnlyHttpFixture(), AgentCredential(token="z" * 43, project_id=project_id)
    )
    operation = agent.call(
        "read_operation_status", {"experiment_id": "experiment_astra", "operation_id": operation_id}
    )
    assert operation.operation_id == operation_id
    assert operation.observed_state == "running"
