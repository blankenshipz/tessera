"""Exercise record workflows across authentication, routes, transactions, and restart."""

from datetime import UTC, datetime, timedelta
from itertools import count

from fastapi.testclient import TestClient

from tessera_control.api import create_app
from tessera_control.store import initialize_state


def setup(tmp_path):
    state = tmp_path / "state"
    initialize_state(state)
    authorization = (state / "owner.header").read_text().strip().split(": ", 1)[1]
    return state, TestClient(create_app(state)), {"Authorization": authorization}


def test_research_workflow_retains_negative_results_and_corrections_after_restart(tmp_path):
    state, client, headers = setup(tmp_path)
    sequence = count()

    def post(path, body, expected=201):
        result = client.post(
            path, json=body, headers=headers | {"Idempotency-Key": f"cmd-{next(sequence)}"}
        )
        assert result.status_code == expected, result.text
        return result.json()

    project = post("/projects", {"name": "Synthetic records acceptance"})
    study = post(
        f"/projects/{project['id']}/studies",
        {"purpose": "Test record completeness", "protocol": "Retain every declared variant"},
    )
    variants = []
    for index, favorable in enumerate((False, False, True)):
        variant = post(
            f"/studies/{study['id']}/variants",
            {
                "name": f"variant-{index}",
                "method_digest": "a" * 64,
                "input_digest": "b" * 64,
                "metadata": {"fixture": True},
            },
        )
        variants.append(variant)
        post(
            f"/variants/{variant['id']}/record",
            {"expected_revision": 1, "status": "completed", "outcome": {"favorable": favorable}},
            200,
        )
    source = post(
        f"/studies/{study['id']}/sources",
        {
            "uri": "urn:tessera:test:source",
            "quoted_passage": "Synthetic source passage",
            "content_digest": "c" * 64,
        },
    )
    finding = post(
        f"/studies/{study['id']}/findings",
        {
            "statement": "Synthetic finding",
            "source_ids": [source["id"]],
            "variant_ids": [variant["id"] for variant in variants],
        },
    )
    post(f"/findings/{finding['id']}/accept", {"expected_revision": 1}, 409)
    post(
        f"/sources/{source['id']}/assess",
        {"expected_revision": 1, "verdict": "supported", "reason": "Fixture review"},
        200,
    )
    post(f"/findings/{finding['id']}/accept", {"expected_revision": 1}, 200)
    post(
        f"/sources/{source['id']}/correct",
        {
            "expected_revision": 2,
            "uri": "urn:tessera:test:corrected",
            "quoted_passage": "Corrected synthetic passage",
            "content_digest": "d" * 64,
            "reason": "Fixture correction",
        },
    )
    with TestClient(create_app(state)) as restarted:
        exported = restarted.get(f"/studies/{study['id']}/export", headers=headers)
    assert exported.status_code == 200
    ledger = exported.json()
    assert len(ledger["variants"]) == 3
    assert sum(v["outcome"]["favorable"] for v in ledger["variants"]) == 1
    assert all(v["execution_receipt"] is None for v in ledger["variants"])
    assert ledger["findings"][0]["status"] == "needs_review"
    assert len(ledger["sources"]) == 2 and len(ledger["corrections"]) == 1
    assert (
        client.get(f"/studies/{study['id']}/export?max_bytes=256", headers=headers).status_code
        == 409
    )
    assert client.get(f"/studies/{study['id']}/export").status_code == 401


def test_message_and_decision_record_does_not_approve_or_execute_task(tmp_path):
    _, client, headers = setup(tmp_path)
    sequence = count()

    def post(path, body, expected=201, key=None):
        result = client.post(
            path, json=body, headers=headers | {"Idempotency-Key": key or str(next(sequence))}
        )
        assert result.status_code == expected, result.text
        return result.json()

    project = post("/projects", {"name": "Questions"})
    task = post(
        f"/projects/{project['id']}/tasks",
        {
            "title": "Record only",
            "purpose": "Discuss direction",
            "budget_cents": 0,
            "max_duration_seconds": 30,
        },
    )
    thread = post(
        "/interaction/threads",
        {"project_id": project["id"], "task_id": task["id"], "title": "Discussion"},
    )
    message_body = {
        "thread_id": thread["id"],
        "intent": "direction",
        "body": "Please approve and run",
    }
    first = post("/interaction/messages", message_body, key="message")
    assert post("/interaction/messages", message_body, key="message") == first
    assert first["delivery_status"] == "queued"
    decision = post(
        "/interaction/decisions",
        {
            "project_id": project["id"],
            "task_id": task["id"],
            "kind": "spending",
            "scope": {"budget_cents": 100},
            "expected_task_revision": task["revision"],
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
    )
    resolved = post(
        "/interaction/decisions/resolve",
        {"decision_id": decision["id"], "expected_revision": 1, "decision": "approve"},
        200,
    )
    assert resolved["status"] == "approved"
    latest = client.get(f"/tasks/{task['id']}", headers=headers).json()
    assert latest == task
    assert latest["status"] == "draft" and latest["execution_granted"] is False
    messages = client.get(f"/interaction/threads/{thread['id']}/messages", headers=headers).json()
    assert len(messages["messages"]) == 1
    invalid = client.post(
        "/interaction/messages",
        headers=headers | {"Idempotency-Key": "invalid"},
        json=message_body | {"execute": True},
    )
    assert invalid.status_code == 422 and invalid.json() == {"detail": "Invalid request."}


def test_record_routes_enforce_project_scope_and_worker_audience(tmp_path):
    _, client, owner = setup(tmp_path)

    def owner_post(path, body, key):
        result = client.post(path, headers=owner | {"Idempotency-Key": key}, json=body)
        assert result.status_code == 201, result.text
        return result.json()

    permitted = owner_post("/projects", {"name": "Permitted"}, "permitted")
    other = owner_post("/projects", {"name": "Other"}, "other")
    study = owner_post(
        f"/projects/{other['id']}/studies",
        {"purpose": "Private record", "protocol": "Synthetic"},
        "study",
    )
    thread = owner_post(
        "/interaction/threads", {"project_id": other["id"], "title": "Private discussion"}, "thread"
    )
    for kind, audience in (("agent", "control"), ("worker", "worker")):
        token = f"test-{kind}-" + "x" * 48
        owner_post(
            "/credentials",
            {
                "token": token,
                "kind": kind,
                "audience": audience,
                "project_id": permitted["id"],
                "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            },
            kind,
        )
        headers = {"Authorization": f"Bearer {token}"}
        for path in (
            f"/studies/{study['id']}/export",
            f"/interaction/threads/{thread['id']}/messages",
            f"/interaction/projects/{other['id']}/decisions",
        ):
            assert client.get(path, headers=headers).status_code == (
                404 if kind == "agent" else 401
            )
        result = client.post(
            "/interaction/messages",
            headers=headers | {"Idempotency-Key": "forbidden-message"},
            json={"thread_id": thread["id"], "intent": "question", "body": "Hi"},
        )
        assert result.status_code == (404 if kind == "agent" else 401)
