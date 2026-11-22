"""Provider input tickets under the provider trust setting (ADR 0040 section 4)."""

# ruff: noqa: F811 - the imported fixtures are used as test arguments

from __future__ import annotations

import json
import sqlite3

from provider_trust_fixture import ALL_CLASSES, DERIVED, install_trust
from test_provider_inputs import (  # noqa: F401
    _issue,
    _make_private,
    chunked,
    tiny_chunks,
)

from tessera_control.provider_trust import current_policy

DENIED = {"detail": "Provider input ticket is not valid."}


def _bind_route(state, op, target):
    """A route-bound operation: the state event the guard reads for its target."""
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        org_id, project_id, task_id, worker_id = db.execute(
            "SELECT org_id, project_id, task_id, worker_principal_id FROM external_operations "
            "WHERE operation_id=?",
            (op,),
        ).fetchone()
        db.execute(
            """INSERT INTO events (org_id, project_id, task_id, event_type, resource_revision,
            actor_principal_id, created_at, payload_json)
            VALUES (?, ?, ?, 'remote_execution.state', 1, ?, '2026-01-01T00:00:00Z', ?)""",
            (
                org_id,
                project_id,
                task_id,
                worker_id,
                json.dumps({"proposal": {"operation_id": op, "selection": {"target": target}}}),
            ),
        )


def _ticket_events(state):
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        return [
            json.loads(row[0])
            for row in db.execute(
                "SELECT payload_json FROM events "
                "WHERE event_type='execution.provider_input_ticket_issued' ORDER BY cursor"
            )
        ]


def test_a_private_input_is_refused_a_ticket_under_the_default_even_on_a_route(chunked):
    state, owner, worker, grant, lease, client, op = chunked
    _bind_route(state, op, "salad_container")
    _make_private(state, grant["snapshot_id"])
    refused = _issue(client, op, lease)
    assert refused.status_code == 403
    assert "'salad'" in refused.json()["detail"] and worker["id"] in refused.json()["detail"]
    assert _ticket_events(state) == []


def test_a_bound_provider_instance_gets_a_ticket_and_the_event_records_the_policy(chunked):
    state, owner, worker, grant, lease, client, op = chunked
    _bind_route(state, op, "salad_container")
    _make_private(state, grant["snapshot_id"])
    policy = install_trust({"salad": (DERIVED, [worker["id"]])})
    assert _issue(client, op, lease).status_code == 201
    (event,) = _ticket_events(state)
    assert event["provider_id"] == "salad" and event["worker_principal_id"] == worker["id"]
    assert event["trust_policy_digest"] == policy.digest
    assert event["admission_basis"] == "provider_trust_policy"
    assert event["input_data_class"] == "private_derived" and event["input_code"] == "none"
    # A principal bound to another provider is public only on this route.
    install_trust({"self-hosted": (ALL_CLASSES, [worker["id"]])})
    assert _issue(client, op, lease).status_code == 403


def test_a_public_input_records_the_public_only_basis(chunked):
    state, owner, worker, grant, lease, client, op = chunked
    assert _issue(client, op, lease).status_code == 201
    (event,) = _ticket_events(state)
    assert event["admission_basis"] == "public_only" and event["provider_id"] is None
    assert event["trust_policy_digest"] == current_policy().digest


def test_a_live_ticket_stops_working_after_the_setting_narrows(chunked):
    state, owner, worker, grant, lease, client, op = chunked
    _bind_route(state, op, "salad_container")
    _make_private(state, grant["snapshot_id"])
    install_trust({"salad": (DERIVED, [worker["id"]])})
    token = _issue(client, op, lease).json()["token"]
    headers = {"Authorization": "Ticket " + token}
    assert client.get("/provider-inputs/manifest", headers=headers).status_code == 200
    assert client.get("/provider-inputs/chunks/0", headers=headers).status_code == 200
    # A reload that no longer lists the principal cuts the ticket off at the next pull.
    install_trust({"salad": (DERIVED, [])})
    for path in ("/provider-inputs/manifest", "/provider-inputs/chunks/1"):
        response = client.get(path, headers=headers)
        assert response.status_code == 401 and response.json() == DENIED
    # And a new ticket is refused.
    assert _issue(client, op, lease).status_code == 403


# ---- the recorded setting is shared across processes (ADR 0040 section 4) ----------------


def _record(state, providers, actor="deployment_start"):
    """What serve does at start and on reload: record only; this process installs nothing."""
    from provider_trust_fixture import build_policy

    from tessera_control.provider_trust import record_at_start, reset_policy

    policy = build_policy(providers)
    record_at_start(state / "tessera.sqlite3", policy, actor)
    reset_policy()  # the issuing process holds no in-memory policy of its own
    return policy


def test_a_policy_recorded_by_another_process_admits_and_a_reload_reaches_it(chunked):
    state, owner, worker, grant, lease, client, op = chunked
    _bind_route(state, op, "salad_container")
    _make_private(state, grant["snapshot_id"])
    assert _issue(client, op, lease).status_code == 403  # nothing recorded: builtin default
    policy = _record(state, {"salad": (DERIVED, [worker["id"]])})
    assert current_policy().source == "builtin"  # this process's own memory is untouched
    assert _issue(client, op, lease).status_code == 201
    (event,) = _ticket_events(state)
    assert event["trust_policy_digest"] == policy.digest
    # A reload in the serving process that drops the principal reaches this one at once.
    _record(state, {"salad": (DERIVED, [])}, actor="principal_operator")
    assert _issue(client, op, lease).status_code == 403


def test_a_fresh_subprocess_reads_the_recorded_policy(chunked, tmp_path):
    import subprocess
    import sys
    import textwrap

    state, owner, worker, grant, lease, client, op = chunked
    _make_private(state, grant["snapshot_id"])
    _record(state, {"salad": (DERIVED, [worker["id"]])})
    script = textwrap.dedent(
        """
        import sys
        from pathlib import Path
        from tessera_control.provider_trust import current_policy, provider_admits
        from tessera_control.store import _connect

        db = _connect(Path(sys.argv[1]) / "tessera.sqlite3")
        print(current_policy().source)  # no connection: this process's own default
        admitted = provider_admits(db, "salad", sys.argv[2], sys.argv[3])
        print(admitted.input_data_class, admitted.basis)
        """
    )
    done = subprocess.run(
        [sys.executable, "-I", "-c", script, str(state), worker["id"], grant["snapshot_id"]],
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.split()[:3] == ["builtin", "private_derived", "provider_trust_policy"]
