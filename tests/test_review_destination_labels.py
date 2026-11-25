"""Review job submit applies the destination's licence labels (ADR 0168, ADR 0040) and records
the admitted destination, data classes, labels and policy digest on the submit event."""

import json
import sqlite3

from fastapi.testclient import TestClient
from test_review_destination_policy import _submit, document, write_policy
from test_review_jobs import job, world  # noqa: F401

from tessera_control.api import create_app


def _label(w, snapshot_id, labels):
    with sqlite3.connect(w.db) as db:
        (org,) = db.execute("SELECT org_id FROM projects WHERE id=?", (w.project,)).fetchone()
        db.execute(
            "INSERT INTO snapshot_license_labels (snapshot_id,org_id,project_id,labels_json,"
            "revision,updated_by_principal_id,updated_at) VALUES (?,?,?,?,1,'p','t')",
            (snapshot_id, org, w.project, json.dumps(labels)),
        )


def _client(w, tmp_path, **entry):
    path = write_policy(tmp_path / "d.json", document(**entry))
    return TestClient(create_app(w.state, review_destinations=path))


def test_non_commercial_input_is_refused_by_default(world, tmp_path):  # noqa: F811
    _label(world, "s1", ["non-commercial"])
    r = _submit(_client(world, tmp_path), world, job(destination_id="anthropic"), "k-nc")
    assert r.status_code == 409
    assert r.json()["reason"] == "data_class_not_allowed_for_destination"
    with sqlite3.connect(world.db) as db:
        assert db.execute("SELECT COUNT(*) FROM review_jobs").fetchone()[0] == 0


def test_a_label_the_destination_lists_is_admitted(world, tmp_path):  # noqa: F811
    _label(world, "s1", ["cc-by"])
    client = _client(world, tmp_path, allowed_license_labels=["unlabelled", "cc-by"])
    r = _submit(client, world, job(destination_id="anthropic"), "k-ccby")
    assert r.status_code == 201, r.text


def test_the_submit_event_records_the_admission(world, tmp_path):  # noqa: F811
    client = _client(world, tmp_path)
    r = _submit(client, world, job(destination_id="anthropic"), "k-event")
    assert r.status_code == 201, r.text
    digest = client.app.state.review_policy.digest
    with sqlite3.connect(world.db) as db:
        (payload,) = db.execute(
            "SELECT payload_json FROM events WHERE event_type='review_job.submitted'"
        ).fetchone()
    event = json.loads(payload)
    assert event["destination_id"] == "anthropic"
    assert event["data_classes"] == ["private_derived"]
    assert event["license_labels"] == ["unlabelled"]
    assert event["policy_digest"] == digest
