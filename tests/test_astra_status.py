# ruff: noqa: E501, E702
# fmt: off
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tessera_agent import (
    AgentCredential,
    AstraAgentFacade,
    ExplainStatusCoordinator,
    ExplainStatusMediator,
)
from tessera_control.api import create_app
from tessera_control.recovery import backup_state, restore_state
from tessera_control.store import _connect, initialize_state, migrate_state


def _headers(state: Path, key: str, token: str | None = None) -> dict[str, str]:
    if token is None:
        _, token = (state / "owner.header").read_text().strip().split("Bearer ", 1)
    return {"Authorization": f"Bearer {token}", "Idempotency-Key": key}


def _project(client: TestClient, state: Path, name: str) -> dict[str, object]:
    response = client.post("/projects", headers=_headers(state, name), json={"name": name})
    assert response.status_code == 201
    return response.json()


def _agent(client: TestClient, state: Path, project_id: str, token: str = "a" * 43) -> str:
    response = client.post(
        "/credentials",
        headers=_headers(state, f"agent-{token[0]}"),
        json={"token": token, "kind": "agent", "audience": "control", "project_id": project_id,
              "expires_at": (datetime.now(UTC) + timedelta(minutes=30)).isoformat()},
    )
    assert response.status_code == 201
    return token


def _experiment(client: TestClient, state: Path, project_id: str) -> dict[str, object]:
    response = client.post(
        f"/projects/{project_id}/experiments", headers=_headers(state, "experiment"), json={
            "goal": "Report a bounded current status.", "success_criterion": "No execution is created.",
            "protocol": "Read the already durable record only.", "variants": [
                {"kind": "baseline", "name": "base", "method_digest": "a" * 64, "input_digest": "b" * 64, "metadata": {}},
                {"kind": "intervention", "name": "change", "method_digest": "c" * 64, "input_digest": "d" * 64, "metadata": {}},
            ],
        },
    )
    assert response.status_code == 201
    return response.json()


class _Control:
    def __init__(self, client: TestClient, state: Path, token: str):
        self.client, self.state, self.token, self.sequence = client, state, token, 0

    def _post(self, path: str, body: dict[str, object]) -> dict[str, object]:
        self.sequence += 1
        response = self.client.post(path, headers=_headers(self.state, f"runtime-{self.sequence}", self.token), json=body)
        assert response.status_code == 200, response.text
        return response.json()

    def claim(self, turn_id: str, expected_revision: int) -> dict[str, object]:
        return self._post(f"/astra/explain-status-turns/{turn_id}/claim", {"expected_revision": expected_revision})

    def begin_tool(
        self, turn_id: str, expected_revision: int, experiment_id: str
    ) -> dict[str, object]:
        # The runtime is deliberately unable to choose another facade tool here.
        assert experiment_id == self.experiment_id
        return self._post(f"/astra/explain-status-turns/{turn_id}/tool-intent", {"expected_revision": expected_revision, "tool": "read_experiment_status", "arguments": {"identifier": experiment_id}})

    def record_result(self, turn_id: str, expected_revision: int, result: dict[str, object]) -> dict[str, object]:
        return self._post(f"/astra/explain-status-turns/{turn_id}/tool-result", {"expected_revision": expected_revision, "result": result})

    def answer(self, turn_id: str, expected_revision: int) -> dict[str, object]:
        return self._post(f"/astra/explain-status-turns/{turn_id}/answer", {"expected_revision": expected_revision})

    def uncertain(self, turn_id: str, expected_revision: int) -> dict[str, object]:
        return self._post(f"/astra/explain-status-turns/{turn_id}/uncertain", {"expected_revision": expected_revision})


def _request(client: TestClient, state: Path, project: dict[str, object], experiment: dict[str, object], key: str = "request", conversation_revision: int = 0) -> dict[str, object]:
    response = client.post(
        f"/astra/projects/{project['id']}/explain-status-turns", headers=_headers(state, key), json={
            "expected_conversation_revision": conversation_revision, "prompt": "What is the recorded status?",
            "experiment_id": experiment["id"], "expected_design_revision": experiment["current_design_revision"],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_explain_status_runtime_persists_one_mediated_tool_and_attributed_answer(tmp_path: Path) -> None:
    state = tmp_path / "state"; initialize_state(state); client = TestClient(create_app(state))
    project = _project(client, state, "Status"); experiment = _experiment(client, state, str(project["id"])); token = _agent(client, state, str(project["id"]))
    requested = _request(client, state, project, experiment)
    class CountingFacade:
        def __init__(self, facade: AstraAgentFacade) -> None:
            self.facade, self.calls = facade, 0

        def call(self, tool: str, arguments: dict[str, object]):
            self.calls += 1
            return self.facade.call(tool, arguments)

    facade = CountingFacade(AstraAgentFacade(client, AgentCredential(token=token, project_id=str(project["id"]))))
    output = ExplainStatusCoordinator(facade, ExplainStatusMediator(client, token)).run(str(requested["turn_id"]), 1)
    assert output["state"] == "completed"
    assert output["binding"]["state"] == "answered"
    assert output["tool_invocation"]["tool_name"] == "read_experiment_status"
    assert output["tool_invocation"]["provenance"]["provider_disabled"] is True
    assert output["tool_invocation"]["provenance"]["model"] == "deterministic-provider-disabled"
    assert output["tool_invocation"]["observed_at"] is not None
    assert output["answer"]["provenance"]["harness"] == "tessera-agent.ExplainStatusCoordinator"
    assert output["answer"]["operation_ids"] == []
    assert "Scientific validity remains not assessed" in output["answer"]["body"]
    assert facade.calls == 1
    from tessera_control.store import _connect

    with _connect(state / "tessera.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM execution_grants").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM external_operations").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM research_experiment_designs").fetchone()[0] == 1


def test_lost_result_ack_restarts_without_a_second_facade_call(tmp_path: Path) -> None:
    """A committed result with a lost acknowledgement resumes at answer only."""
    state = tmp_path / "state"; initialize_state(state); client = TestClient(create_app(state))
    project = _project(client, state, "Status"); experiment = _experiment(client, state, str(project["id"]))
    token = _agent(client, state, str(project["id"])); requested = _request(client, state, project, experiment)

    class CountingFacade:
        def __init__(self) -> None:
            self.inner = AstraAgentFacade(client, AgentCredential(token=token, project_id=str(project["id"])))
            self.calls = 0

        def call(self, tool: str, arguments: dict[str, object]):
            self.calls += 1
            return self.inner.call(tool, arguments)

    class LostResultAck(ExplainStatusMediator):
        def record_result(self, turn_id: str, expected_revision: int, result: dict[str, object]) -> dict[str, object]:
            super().record_result(turn_id, expected_revision, result)
            raise ConnectionError("simulated lost result acknowledgement")

    facade = CountingFacade()
    with pytest.raises(ConnectionError, match="lost result"):
        ExplainStatusCoordinator(facade, LostResultAck(client, token)).run(str(requested["turn_id"]), 1)
    recovered = ExplainStatusCoordinator(facade, ExplainStatusMediator(client, token)).run(
        str(requested["turn_id"]), 1
    )
    assert recovered["state"] == "completed"
    assert recovered["binding"]["state"] == "answered"
    assert facade.calls == 1


def test_expired_or_revoked_binding_cannot_reach_the_status_tool(tmp_path: Path) -> None:
    state = tmp_path / "state"; initialize_state(state); client = TestClient(create_app(state))
    project = _project(client, state, "Status"); experiment = _experiment(client, state, str(project["id"]))
    token = _agent(client, state, str(project["id"])); requested = _request(client, state, project, experiment)
    next_requested = _request(client, state, project, experiment, "request-next", 1)
    claimed = client.post(
        f"/astra/explain-status-turns/{requested['turn_id']}/claim",
        headers=_headers(state, "claim", token), json={"expected_revision": 1},
    )
    binding = claimed.json()["binding"]
    from tessera_control.store import _connect

    with _connect(state / "tessera.sqlite3") as connection:
        connection.execute(
            "UPDATE astra_status_discussion_bindings SET lease_expires_at=? WHERE id=?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), binding["id"]),
        )
    expired = client.post(
        f"/astra/explain-status-turns/{requested['turn_id']}/tool-intent",
        headers=_headers(state, "expired", token),
        json={"expected_revision": binding["revision"], "tool": "read_experiment_status", "arguments": {"identifier": experiment["id"]}},
    )
    assert expired.status_code == 409
    assert client.get(f"/astra/explain-status-turns/{requested['turn_id']}", headers=_headers(state, "read")).json()["binding"]["state"] == "expired"
    credential = client.get("/me", headers=_headers(state, "me", token)).json()["credential_id"]
    assert client.post(f"/credentials/{credential}/revoke", headers=_headers(state, "revoke"), json={}).status_code == 200
    revoked = client.post(
        f"/astra/explain-status-turns/{next_requested['turn_id']}/claim",
        headers=_headers(state, "revoked", token), json={"expected_revision": 1},
    )
    assert revoked.status_code == 401


def test_status_turn_denies_foreign_unknown_stale_revoked_retry_and_cancellation(tmp_path: Path) -> None:
    state = tmp_path / "state"; initialize_state(state); client = TestClient(create_app(state))
    project = _project(client, state, "Status"); other = _project(client, state, "Other")
    experiment = _experiment(client, state, str(project["id"])); token = _agent(client, state, str(project["id"]))
    foreign = _agent(client, state, str(other["id"]), "b" * 43)
    requested = _request(client, state, project, experiment)
    turn_id = requested["turn_id"]
    assert client.post(f"/astra/explain-status-turns/{turn_id}/claim", headers=_headers(state, "foreign", foreign), json={"expected_revision": 1}).status_code == 404
    claimed = client.post(f"/astra/explain-status-turns/{turn_id}/claim", headers=_headers(state, "claim", token), json={"expected_revision": 1})
    assert claimed.status_code == 200
    binding = claimed.json()["binding"]
    bad = client.post(f"/astra/explain-status-turns/{turn_id}/tool-intent", headers=_headers(state, "unknown", token), json={"expected_revision": binding["revision"], "tool": "read_task", "arguments": {"identifier": experiment["id"]}})
    assert bad.status_code == 422
    intent_body = {"expected_revision": binding["revision"], "tool": "read_experiment_status", "arguments": {"identifier": experiment["id"]}}
    intent = client.post(f"/astra/explain-status-turns/{turn_id}/tool-intent", headers=_headers(state, "intent", token), json=intent_body)
    assert intent.status_code == 200
    assert client.post(f"/astra/explain-status-turns/{turn_id}/tool-intent", headers=_headers(state, "intent", token), json=intent_body).json() == intent.json()
    assert client.post(f"/astra/explain-status-turns/{turn_id}/tool-intent", headers=_headers(state, "intent-again", token), json=intent_body).status_code == 409
    cancelled = client.post(f"/astra/explain-status-turns/{turn_id}/cancel", headers=_headers(state, "cancel"), json={"expected_revision": 2})
    assert cancelled.status_code == 200 and cancelled.json()["binding"]["state"] == "cancelled"
    assert client.post(f"/astra/explain-status-turns/{turn_id}/uncertain", headers=_headers(state, "late", token), json={"expected_revision": 2}).status_code == 409
    stale = client.post(f"/astra/projects/{project['id']}/explain-status-turns", headers=_headers(state, "stale"), json={"expected_conversation_revision": 2, "prompt": "stale", "experiment_id": experiment["id"], "expected_design_revision": 99})
    assert stale.status_code == 409
    credential = client.get("/me", headers=_headers(state, "me", token)).json()["credential_id"]
    assert client.post(f"/credentials/{credential}/revoke", headers=_headers(state, "revoke"), json={}).status_code == 200
    assert client.get(f"/astra/explain-status-turns/{turn_id}", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_v41_to_v42_migration_is_empty_repeatable_and_recoverable(tmp_path: Path) -> None:
    state = tmp_path / "state"; initialize_state(state)
    with _connect(state / "tessera.sqlite3") as connection:
        for table in (
            "astra_status_answers",
            "astra_status_tool_invocations",
            "astra_status_discussion_bindings",
            "astra_status_discussions",
        ):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("UPDATE schema_meta SET schema_version=41")

    assert migrate_state(state) == 43
    assert migrate_state(state) == 43
    with _connect(state / "tessera.sqlite3") as connection:
        for table in (
            "astra_status_discussions",
            "astra_status_discussion_bindings",
            "astra_status_tool_invocations",
            "astra_status_answers",
        ):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        assert connection.execute("PRAGMA foreign_key_check").fetchone() is None

    client = TestClient(create_app(state))
    project = _project(client, state, "Migrated status")
    experiment = _experiment(client, state, str(project["id"]))
    token = _agent(client, state, str(project["id"]))
    requested = _request(client, state, project, experiment)
    output = ExplainStatusCoordinator(
        AstraAgentFacade(client, AgentCredential(token=token, project_id=str(project["id"]))),
        ExplainStatusMediator(client, token),
    ).run(str(requested["turn_id"]), 1)
    assert output["state"] == "completed"

    backup, restored = tmp_path / "backup", tmp_path / "restored"
    assert backup_state(state, backup)["schema_version"] == 43
    restore_state(backup, restored)
    recovered = TestClient(create_app(restored)).get(
        f"/astra/explain-status-turns/{requested['turn_id']}",
        headers=_headers(restored, "recovered-status"),
    )
    assert recovered.status_code == 200
    assert recovered.json() == output
