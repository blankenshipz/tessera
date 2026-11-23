"""Provider input tickets for private_derived snapshot releases (ADR 0040, ADR 0113).

A Salad-routed operation whose input snapshot is released ``private_derived`` gets a ticket
only when the route's provider instance is trusted with that class and its licence-label
rule admits the snapshot's labels. A public input mints as before.
"""

# ruff: noqa: F811 - the imported fixtures are used as test arguments

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from provider_trust_fixture import DERIVED, install_trust
from test_chunked_snapshots import FILES, _state, _upload
from test_execution import _grant_payload, _task, _worker
from test_provider_inputs import (  # noqa: F401
    _issue,
    _make_private,
    _operation,
    _rows,
    tiny_chunks,
)
from test_provider_inputs_trust import _bind_route, _ticket_events

from tessera_control import license_labels
from tessera_control.api import create_app
from tessera_control.execution import ExecutionStore
from tessera_control.provider_trust import install_policy, policy_from_document
from tessera_control.snapshot_chunks import ChunkedSnapshotStore
from tessera_control.snapshot_releases import SnapshotReleaseStore
from tessera_control.store import _connect, canonical_digest

REFUSED = "Provider input refused"


def _release(state, owner, worker_id, snapshot, classification):
    payload = {
        "expected_digest": snapshot["digest"],
        "worker_principal_id": worker_id,
        "classification": classification,
        "permits_worker_transfer": True,
        "rationale": "Synthetic provider input release.",
        "expires_at": (datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
    }
    status, release = SnapshotReleaseStore(state).release_snapshot(
        owner, snapshot["id"], "release", canonical_digest(payload), payload
    )
    assert status == 201 and release["classification"] == classification
    return release


def _salad_case(tmp_path, classification):
    """A chunked snapshot released ``classification`` to a Salad-bound worker, a granted
    lease, and an external operation routed to ``salad_container``."""
    state, owner, project = _state(tmp_path)
    worker = _worker(state, owner, project["id"], "worker-token")
    # The worker principal is bound to Salad for private_derived, so the local grant and
    # lease admit the private release (ADR 0040 section 4).
    install_trust({"salad": (DERIVED, [worker["id"]])})
    snapshot, _ = _upload(ChunkedSnapshotStore(state), owner, project["id"], FILES)
    _release(state, owner, worker["id"], snapshot, classification)
    task = _task(state, owner, project["id"], snapshot["digest"], "First")
    execution = ExecutionStore(state)
    limits = {
        "expected_revision": 0,
        "currency": "USD",
        "monetary_limit_cents": 100,
        "max_concurrency": 1,
    }
    execution.configure_project_execution(
        owner, project["id"], "limits", canonical_digest(limits), limits
    )
    payload = _grant_payload(task, worker, {"id": snapshot["id"]})
    _, grant = execution.issue_execution_grant(
        owner, project["id"], "grant", canonical_digest(payload), payload
    )
    assert grant["snapshot_release_id"]
    _, lease = execution.acquire_execution_lease(worker, grant["id"], "lease", "lease")
    client = TestClient(create_app(state))
    op = _operation(state, worker, lease)
    _bind_route(state, op, "salad_container")
    if classification == "private_derived":
        _make_private(state, grant["snapshot_id"])
    return state, owner, worker, grant, lease, client, op


@pytest.fixture
def private_case(tmp_path):
    return _salad_case(tmp_path, "private_derived")


@pytest.fixture
def public_case(tmp_path):
    return _salad_case(tmp_path, "public")


def _trust_with_labels(worker_id, classes, allowed_labels):
    """Salad trusted for ``classes`` with an ``allowed_license_labels`` restriction."""
    document = {
        "schema": "tessera.provider-trust.v1",
        "providers": {
            "salad": {
                "data_classes": list(classes),
                "worker_principal_ids": [worker_id],
                "allowed_license_labels": list(allowed_labels),
            }
        },
    }
    policy = policy_from_document(document, "file")
    install_policy(policy)
    return policy


def _label(state, owner, snapshot_id, labels):
    db = _connect(state / "tessera.sqlite3")
    try:
        license_labels.add_snapshot_labels(db, owner, snapshot_id, labels, "2026-01-01T00:00:00Z")
    finally:
        db.close()


def _manifest(client, token):
    return client.get("/provider-inputs/manifest", headers={"Authorization": f"Ticket {token}"})


# ---- (a) private_derived, provider trusted ------------------------------------------------


def test_a_private_derived_release_mints_a_ticket_for_a_trusted_provider(private_case):
    state, owner, worker, grant, lease, client, op = private_case
    issued = _issue(client, op, lease)
    assert issued.status_code == 201, issued.text
    body = issued.json()
    assert body["token"] and body["snapshot_digest"] == grant["snapshot_digest"]
    (row,) = _rows(state)
    assert row["operation_id"] == op and row["snapshot_id"] == grant["snapshot_id"]
    assert row["revoked_at"] is None
    (event,) = _ticket_events(state)
    assert event["provider_id"] == "salad" and event["worker_principal_id"] == worker["id"]
    assert event["input_data_class"] == "private_derived"
    assert event["admission_basis"] == "provider_trust_policy"
    # The ticket also serves its pulls (each pull re-checks the same gates).
    assert _manifest(client, body["token"]).status_code == 200


# ---- (b) private_derived, provider not trusted for the class ------------------------------


def test_a_private_derived_release_is_refused_when_the_route_provider_is_not_trusted(
    private_case,
):
    """The worker principal is trusted with private_derived, but under another provider:
    the route's Salad instance receives public data only, so the ticket gate refuses."""
    state, owner, worker, grant, lease, client, op = private_case
    install_trust({"local-docker": (DERIVED, [worker["id"]])})
    refused = _issue(client, op, lease)
    assert refused.status_code == 403
    detail = refused.json()["detail"]
    assert detail.startswith(REFUSED) and "'salad'" in detail, detail
    assert "not approved for this local CPU authority" not in detail
    assert _rows(state) == [] and _ticket_events(state) == []


@pytest.mark.parametrize(
    "providers",
    [
        pytest.param(lambda w: {"salad": (["public"], [w])}, id="salad-public-only"),
        pytest.param(lambda w: {"salad": (DERIVED, ["someone-else"])}, id="principal-unbound"),
    ],
)
def test_a_private_derived_release_is_refused_after_the_setting_narrows(private_case, providers):
    """Narrowing after the lease leaves the principal without private_derived at all; the
    lease fence refuses the release first (fail closed) and nothing is minted."""
    state, owner, worker, grant, lease, client, op = private_case
    install_trust(providers(worker["id"]))
    refused = _issue(client, op, lease)
    assert refused.status_code == 403, refused.text
    assert _rows(state) == [] and _ticket_events(state) == []


# ---- (c) licence labels (ADR 0113) --------------------------------------------------------


def test_a_restricted_licence_label_is_refused_a_ticket(private_case):
    state, owner, worker, grant, lease, client, op = private_case
    _label(state, owner, grant["snapshot_id"], ["noncommercial"])
    _trust_with_labels(worker["id"], DERIVED, ["unlabelled", "open-x"])
    refused = _issue(client, op, lease)
    assert refused.status_code == 403
    detail = refused.json()["detail"]
    assert detail.startswith(REFUSED), detail
    assert "licence labels" in detail and "noncommercial" in detail
    assert _rows(state) == [] and _ticket_events(state) == []


def test_an_allowed_licence_label_mints_a_ticket(private_case):
    state, owner, worker, grant, lease, client, op = private_case
    _label(state, owner, grant["snapshot_id"], ["noncommercial"])
    _trust_with_labels(worker["id"], DERIVED, ["noncommercial", "unlabelled"])
    issued = _issue(client, op, lease)
    assert issued.status_code == 201, issued.text
    assert issued.json()["token"]
    assert len(_rows(state)) == 1


def test_an_unlabelled_snapshot_needs_unlabelled_in_the_allowed_list(private_case):
    state, owner, worker, grant, lease, client, op = private_case
    _trust_with_labels(worker["id"], DERIVED, ["open-x"])
    refused = _issue(client, op, lease)
    assert refused.status_code == 403
    assert refused.json()["detail"].startswith(REFUSED) and "unlabelled" in refused.text
    assert _rows(state) == []
    _trust_with_labels(worker["id"], DERIVED, ["open-x", "unlabelled"])
    assert _issue(client, op, lease).status_code == 201
    assert len(_rows(state)) == 1


def test_a_label_added_after_issue_stops_the_live_ticket(private_case):
    state, owner, worker, grant, lease, client, op = private_case
    _trust_with_labels(worker["id"], DERIVED, ["unlabelled"])
    token = _issue(client, op, lease).json()["token"]
    assert _manifest(client, token).status_code == 200
    _label(state, owner, grant["snapshot_id"], ["restricted-x"])
    response = _manifest(client, token)
    assert response.status_code == 401
    assert response.json() == {"detail": "Provider input ticket is not valid."}


# ---- (d) public regression ----------------------------------------------------------------


def test_a_public_release_still_mints_a_ticket(public_case):
    state, owner, worker, grant, lease, client, op = public_case
    issued = _issue(client, op, lease)
    assert issued.status_code == 201, issued.text
    assert issued.json()["token"]
    (row,) = _rows(state)
    assert row["snapshot_id"] == grant["snapshot_id"]
    (event,) = _ticket_events(state)
    assert event["input_data_class"] == "public"
    assert _manifest(client, issued.json()["token"]).status_code == 200
