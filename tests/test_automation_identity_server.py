"""Owner automation identity on the server: the delegation reference and the upload refusal.

An owner credential adopting dataset bytes must say it is automation (``X-Tessera-Automation``);
it may also name the delegation it acts under (``X-Tessera-Delegation``). Both are labels, never
authority, and the adoption event records who stood behind it. These tests opt out of the suite's
automatic owner label (``real_owner_trust``) so they see the production behaviour."""

from __future__ import annotations

import json
import sqlite3

import pytest
from test_dataset_snapshot_api import command, dataset  # noqa: F401  (fixture)

from tessera_control.approval_trust import (
    AUTOMATION_HEADER,
    DELEGATION_HEADER,
    decision_provenance,
)
from tessera_control.store import DATABASE_NAME


@pytest.fixture
def strict(dataset, monkeypatch):  # noqa: F811
    """The dataset fixture, with the suite's automatic owner label removed after its setup."""
    from tessera_control.approval_trust import AUTOMATION_KEY
    from tessera_control.store import Store

    labelled = Store.authenticate

    def authenticate(self, token):
        actor = labelled(self, token)
        if actor.get(AUTOMATION_KEY) == "pytest":
            del actor[AUTOMATION_KEY]
        return actor

    monkeypatch.setattr(Store, "authenticate", authenticate)
    return dataset


def _post(r, headers, key):
    return r["client"].post(
        r["path"], headers={**r["owner"], **headers, "Idempotency-Key": key}, json=r["body"]
    )


def _adopted_events(r):
    with sqlite3.connect(r["state"] / DATABASE_NAME) as db:
        rows = db.execute(
            "SELECT payload_json FROM events WHERE event_type='workspace.snapshot_adopted'"
        ).fetchall()
    events = [json.loads(row[0]) for row in rows]
    # The fixture's own setup adopts a snapshot under the suite's automatic label; ignore that.
    return [event for event in events if event.get("automation_label") != "pytest"]


def test_a_bare_owner_credential_cannot_adopt_dataset_bytes(strict):
    response = _post(strict, {}, "bare")
    assert response.status_code == 403
    assert AUTOMATION_HEADER in response.text
    assert _adopted_events(strict) == []


def test_a_labelled_owner_adopts_and_the_event_records_label_and_delegation(strict):
    headers = {AUTOMATION_HEADER: "snapshot-upload", DELEGATION_HEADER: "delegation-42"}
    response = _post(strict, headers, "labelled")
    assert response.status_code == 201, response.text
    (event,) = _adopted_events(strict)
    assert event["credential_kind"] == "local_owner"
    assert event["decision_class"] == "owner_automation_labelled"
    assert event["automation"] is True
    assert event["automation_label"] == "snapshot-upload"
    assert event["delegation_ref"] == "delegation-42"


def test_a_label_without_a_delegation_records_none(strict):
    assert _post(strict, {AUTOMATION_HEADER: "snapshot-upload"}, "nodel").status_code == 201
    (event,) = _adopted_events(strict)
    assert "delegation_ref" not in event and event["automation_label"] == "snapshot-upload"


def test_a_delegation_reference_alone_is_refused(strict):
    response = _post(strict, {DELEGATION_HEADER: "delegation-42"}, "alone")
    assert response.status_code in (400, 422), response.text
    assert AUTOMATION_HEADER in response.text
    assert _adopted_events(strict) == []


@pytest.mark.parametrize("value", ["", " padded ", "x" * 129, "bad\x07label"])
def test_an_invalid_delegation_reference_is_refused(strict, value):
    try:
        response = _post(
            strict, {AUTOMATION_HEADER: "snapshot-upload", DELEGATION_HEADER: value}, "bad"
        )
    except Exception:  # the HTTP client itself may refuse an illegal header value
        return
    assert response.status_code in (400, 422), response.text
    assert _adopted_events(strict) == []


def test_a_human_session_needs_no_label(strict):
    from datetime import UTC, datetime, timedelta

    token = "human-session-" + "h" * 40
    command(
        strict["client"],
        strict["owner"],
        "/credentials",
        {
            "token": token,
            "kind": "human_session",
            "audience": "control",
            "project_id": strict["project"]["id"],
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
        key="issue-human",
    )
    human = {"Authorization": "Bearer " + token}
    command(strict["client"], human, strict["path"], strict["body"], "human")
    (event,) = _adopted_events(strict)
    assert event["decision_class"] == "human_session" and "automation_label" not in event


def test_provenance_carries_the_delegation_only_beside_a_label():
    actor = {"kind": "local_owner", "automation_label": "helper", "delegation_ref": "d-1"}
    assert decision_provenance(actor)["delegation_ref"] == "d-1"
    assert "delegation_ref" not in decision_provenance(
        {"kind": "local_owner", "delegation_ref": "d-1"}
    )


def test_the_receipt_records_the_delegation_the_approving_helper_named(tmp_path):
    """The approval receipt names the owner delegation; without one the receipt is unchanged."""
    from campaign_fixture import Env

    from tessera_control.campaign_receipts import build_receipt

    def approval_section(actor_extra):
        root = tmp_path / ("with" if actor_extra else "without")
        root.mkdir()
        env = Env(root)
        campaign, _approval = env.approved({**env.owner, **actor_extra})
        with env.store.runner_connection() as connection:
            row = connection.execute(
                "SELECT * FROM campaigns WHERE id=?", (campaign["id"],)
            ).fetchone()
            return build_receipt(connection, row, "running")[0]["approval"]

    named = approval_section({"delegation_ref": "owner-delegation-7"})
    assert named["automation_label"] == "pytest" and named["delegation_ref"] == "owner-delegation-7"
    assert "delegation_ref" not in approval_section({})
