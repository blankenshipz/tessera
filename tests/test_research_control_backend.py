"""Actual temporary HTTP/SQLite authority receipt; no executor or inference."""

from test_agent_authority import admit_payload, issue, post, prepared  # noqa: F401

from tessera_agent.research_control_receipts import consumption_reference


def test_real_backend_consumption_preserves_actual_attempt_and_replay_identity(request):
    setup = request.getfixturevalue("prepared")
    _, client, _, agent, _, bounds, _ = setup
    grant = issue(setup)
    payload = admit_payload(bounds)
    path = f"/agent-authority-grants/{grant['id']}/consume"
    response = post(client, agent, path, payload, key="research-exact-consumption")
    reference = consumption_reference(response, grant["id"], "attempt:admit")
    assert reference["consumption_id"] == response["consumption_id"]
    assert reference["result_id"] == response["result"]["id"]
    assert reference["consumption_id"] != reference["result_id"]
    assert post(client, agent, path, payload, key="research-exact-consumption") == response


def test_native_status_reader_uses_admitted_state_not_a_fabricated_status_field(request):
    from tessera_agent.research_backend_ports import BackendExperimentReader

    setup = request.getfixturevalue("prepared")
    _, client, _, agent, _, bounds, _ = setup
    grant = issue(setup)
    path = f"/agent-authority-grants/{grant['id']}/consume"
    admitted = post(client, agent, path, admit_payload(bounds), key="native-status-admit")["result"]
    payload = {
        "expected_revision": 1,
        "expected_epoch": 1,
        "model": bounds["model"],
        "command": {"action": "attempt:status", "attempt_id": admitted["id"]},
    }
    response = post(client, agent, path, payload, key="native-status-read", expected=200)
    reader = BackendExperimentReader(None, None)
    assert reader.status(response, admitted["id"]) == "queued"
