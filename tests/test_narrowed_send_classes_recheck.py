"""A campaign stage's narrowing applies at ticket issue and at each pull, as at release.

ADR 0040 section 4: the stage's ``send_classes`` and the licence-label rule only narrow
what the deployment trust setting allows. Before the runner records the operation on the
attempt, the attempt is linked through its grant (or the release that grant delivers), so
the worker's first ticket is already narrowed. Synthetic state; no provider is contacted.
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient
from provider_trust_fixture import DERIVED
from test_campaign_models_v2 import _spec2, _stage2
from test_chunked_snapshots import _lease as _chunked_lease
from test_provider_inputs import _first_chunk, _issue, _make_private, _manifest, _operation

from tessera_control import data_class
from tessera_control.api import create_app
from tessera_control.provider_trust import install_policy, policy_from_document

INVALID = {"detail": "Provider input ticket is not valid."}
NOW = "2026-01-01T00:00:00Z"


@pytest.fixture
def salad(tmp_path, monkeypatch):
    """A provider-input operation whose route targets Salad (third-party compute)."""
    state, owner, project, worker, snapshot, grant, lease = _chunked_lease(tmp_path)
    client = TestClient(create_app(state))
    op = _operation(state, worker, lease)  # recorded public with evidence
    monkeypatch.setattr(
        data_class, "route_target_for_operation", lambda _c, _org, _op: "salad_container"
    )
    return state, owner, project, worker, snapshot, grant, lease, client, op


def _link_campaign(state, owner, project, snapshot, grant, *, send_classes, by="grant"):
    """Record the campaign attempt as the runner does before the worker's first ticket: the
    grant (or only the release, as after a crash before the grant was recorded), not yet
    the operation id."""
    extra = {} if send_classes is None else {"send_classes": send_classes}
    source = {
        "kind": "snapshot",
        "name": "data",
        "snapshot_id": snapshot["id"],
        "digest": snapshot["digest"],
        "data_class": "public",
    }
    spec = _spec2(_stage2("s1", source, **extra))
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute(
            """INSERT INTO campaigns (id, org_id, project_id, spec_json, spec_digest, status,
            budget_cap_cents, revision, submitted_by_principal_id, created_at, updated_at)
            VALUES ('camp1', ?, ?, ?, ?, 'running', 10, 1, ?, ?, ?)""",
            (owner["org_id"], project["id"], json.dumps(spec), "c" * 64, owner["id"], NOW, NOW),
        )
        db.execute(
            """INSERT INTO campaign_stage_attempts (id, campaign_id, stage_key, attempt, status,
            hold_cents, release_id, grant_id, operation_id, revision, created_at, updated_at,
            provider_id) VALUES ('att1', 'camp1', 's1', 1, 'granted', 0, ?, ?, NULL, 1, ?, ?,
            'salad')""",
            (grant["snapshot_release_id"], grant["id"] if by == "grant" else None, NOW, NOW),
        )


def _label(state, owner, snapshot_id, *labels):
    from tessera_control.license_labels import add_snapshot_labels

    with sqlite3.connect(state / "tessera.sqlite3") as db:
        add_snapshot_labels(db, owner, snapshot_id, labels, NOW)


def _trust(worker_id, classes, labels=None):
    entry = {"data_classes": list(classes), "worker_principal_ids": [worker_id]}
    if labels is not None:
        entry["allowed_license_labels"] = list(labels)
    document = {"schema": "tessera.provider-trust.v1", "providers": {"salad": entry}}
    install_policy(policy_from_document(document, "file"))


def _pulls(client, token):
    return [_manifest(client, token).status_code, _first_chunk(client, token).status_code]


# ---------------------------------------------------------------- send_classes


@pytest.mark.parametrize("by", ["grant", "release"])
def test_stage_narrowed_below_the_input_class_is_refused_at_ticket_issue(salad, by):
    state, owner, project, worker, snapshot, grant, lease, client, op = salad
    _trust(worker["id"], DERIVED)
    _make_private(state, snapshot["id"])
    _link_campaign(state, owner, project, snapshot, grant, send_classes=["public"], by=by)
    refused = _issue(client, op, lease)
    assert refused.status_code == 403 and "private_derived" in refused.json()["detail"]


def test_a_class_change_after_issue_is_refused_at_the_next_pull(salad):
    state, owner, project, worker, snapshot, grant, lease, client, op = salad
    _trust(worker["id"], DERIVED)
    _link_campaign(state, owner, project, snapshot, grant, send_classes=["public"])
    token = _issue(client, op, lease).json()["token"]
    assert _pulls(client, token) == [200, 200]
    # The deployment trust setting still admits private_derived; the stage does not.
    _make_private(state, snapshot["id"])
    for response in (_manifest(client, token), _first_chunk(client, token)):
        assert response.status_code == 401 and response.json() == INVALID


def test_narrowing_recorded_after_issue_stops_the_next_pull(salad):
    state, owner, project, worker, snapshot, grant, lease, client, op = salad
    _trust(worker["id"], DERIVED)
    _make_private(state, snapshot["id"])
    token = _issue(client, op, lease).json()["token"]  # not yet linked to a campaign
    assert _pulls(client, token) == [200, 200]
    _link_campaign(state, owner, project, snapshot, grant, send_classes=["public"])
    assert _manifest(client, token).status_code == 401
    assert _first_chunk(client, token).status_code == 401


def test_a_wider_narrowing_never_widens_the_deployment_trust(salad):
    state, owner, project, worker, snapshot, grant, lease, client, op = salad
    # No trust file: Salad receives public only, whatever the stage names.
    _make_private(state, snapshot["id"])
    _link_campaign(
        state, owner, project, snapshot, grant, send_classes=["public", "private_derived"]
    )
    assert _issue(client, op, lease).status_code == 403
    narrowed, _ = data_class.campaign_input_narrowing(
        sqlite3.connect(state / "tessera.sqlite3"), op
    )
    assert narrowed == frozenset({"public", "private_derived"})


@pytest.mark.parametrize("linked", [False, True])
def test_unnarrowed_stage_and_unlinked_operation_keep_deployment_trust_alone(salad, linked):
    """An operation no campaign attempt links to (a human grant, or anything recorded
    before stage narrowing was re-checked) and a stage without send_classes behave exactly
    as before: the deployment trust setting alone decides."""
    state, owner, project, worker, snapshot, grant, lease, client, op = salad
    if linked:
        _link_campaign(state, owner, project, snapshot, grant, send_classes=None)
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        assert data_class.campaign_input_narrowing(db, op) == (None, None)
    _make_private(state, snapshot["id"])
    assert _issue(client, op, lease).status_code == 403  # public only without a file
    _trust(worker["id"], DERIVED)
    token = _issue(client, op, lease).json()["token"]
    assert _pulls(client, token) == [200, 200]


# ---------------------------------------------------------------- licence labels


@pytest.mark.parametrize("send_classes", [None, ["public"], ["public", "private_derived"]])
@pytest.mark.parametrize("label", ["non-commercial", "no-redistribution"])
def test_a_restricted_label_never_reaches_salad_whatever_the_narrowing(salad, send_classes, label):
    state, owner, project, worker, snapshot, grant, lease, client, op = salad
    _trust(worker["id"], DERIVED, labels=["cc-by", "public-domain"])
    _label(state, owner, snapshot["id"], label)
    _link_campaign(state, owner, project, snapshot, grant, send_classes=send_classes)
    refused = _issue(client, op, lease)
    assert refused.status_code == 403 and label in refused.json()["detail"]


def test_a_label_added_after_issue_stops_the_next_pull(salad):
    state, owner, project, worker, snapshot, grant, lease, client, op = salad
    _trust(worker["id"], DERIVED, labels=["cc-by"])
    _label(state, owner, snapshot["id"], "cc-by")
    _link_campaign(state, owner, project, snapshot, grant, send_classes=["public"])
    token = _issue(client, op, lease).json()["token"]
    assert _pulls(client, token) == [200, 200]
    _label(state, owner, snapshot["id"], "non-commercial")  # labels are add-only
    for response in (_manifest(client, token), _first_chunk(client, token)):
        assert response.status_code == 401 and response.json() == INVALID


def test_an_unlisted_provider_receives_no_labelled_campaign_input(salad):
    """Without a trust file Salad is unlisted, so it may receive only unlabelled data."""
    state, owner, project, worker, snapshot, grant, lease, client, op = salad
    _label(state, owner, snapshot["id"], "non-commercial")
    _link_campaign(state, owner, project, snapshot, grant, send_classes=None)
    refused = _issue(client, op, lease)
    assert refused.status_code == 403 and "non-commercial" in refused.json()["detail"]
