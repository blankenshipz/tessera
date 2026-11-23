"""Licence-label routes: a person adds labels (add-only), anyone with snapshot:read reads them."""

from __future__ import annotations

import json

import pytest
from test_campaign_stuck_api import World

LABELS = {"add": ["cc-by", "share-alike"]}


class Labels(World):
    def __init__(self, tmp_path):
        super().__init__(tmp_path)
        self.snapshot = self.env.snapshot_id
        self.route = f"/projects/{self.project}/snapshots/{self.snapshot}/license-labels"

    def post(self, body=LABELS, headers=None, route=None, key=None):
        return self.client.post(
            route or self.route,
            json=body,
            headers={**(headers or self.human()), **(key or self.key())},
        )

    def get(self, headers=None, route=None):
        return self.client.get(route or self.route, headers=headers or self.human())

    def events(self):
        return [
            json.loads(r[0])
            for r in self.sql(
                "SELECT payload_json FROM events WHERE event_type='snapshot.license_labels_added'"
            )
        ]


@pytest.fixture
def world(tmp_path):
    return Labels(tmp_path)


def test_an_unlabelled_snapshot_reads_as_unlabelled(world):
    answer = world.get().json()
    assert answer == {
        "snapshot_id": world.snapshot,
        "labels": ["unlabelled"],
        "labelled": False,
    }


def test_a_person_adds_labels_and_the_read_shows_them_with_an_event(world):
    response = world.post()
    assert response.status_code == 200
    assert response.json() == {
        "snapshot_id": world.snapshot,
        "labels": ["cc-by", "share-alike"],
        "added": ["cc-by", "share-alike"],
        "revision": 1,
    }
    assert world.get().json() == {
        "snapshot_id": world.snapshot,
        "labels": ["cc-by", "share-alike"],
        "labelled": True,
    }
    (event,) = world.events()
    assert event["snapshot_id"] == world.snapshot and event["added"] == ["cc-by", "share-alike"]


def test_labels_only_ever_grow_and_a_repeat_changes_nothing(world):
    world.post()
    again = world.post()  # a new idempotency key, nothing new to add
    assert again.json()["added"] == [] and again.json()["revision"] == 1
    more = world.post({"add": ["attribution"]})
    assert more.json()["labels"] == ["attribution", "cc-by", "share-alike"]
    assert more.json()["revision"] == 2
    assert len(world.events()) == 2  # the no-op wrote no event
    assert world.client.delete(world.route, headers=world.human()).status_code in {404, 405}


def test_an_idempotent_replay_returns_the_stored_answer(world):
    key = {"Idempotency-Key": "fixed-key"}
    first = world.post(key=key)
    second = world.post(key=key)
    assert first.json() == second.json() and len(world.events()) == 1


def test_an_agent_can_read_with_snapshot_read_but_never_write(world):
    reader = world.agent(["snapshot:read"])
    world.post()
    assert world.get(reader).json()["labels"] == ["cc-by", "share-alike"]
    assert world.post({"add": ["x-label"]}, headers=reader).status_code == 403
    assert world.get(world.agent(["campaign:read"])).status_code == 403
    assert world.get(world.agent(["snapshot:read"])).status_code == 200


def test_another_organization_and_another_project_see_not_found(world):
    other = world.other_org_human()
    assert world.get(other).status_code in {403, 404}
    assert world.post(headers=other).status_code in {403, 404}
    wrong = f"/projects/proj_wrong/snapshots/{world.snapshot}/license-labels"
    assert world.get(route=wrong).status_code in {403, 404}
    assert world.post(route=wrong).status_code in {403, 404}
    unknown = f"/projects/{world.project}/snapshots/snapshot_nope/license-labels"
    assert (
        world.get(route=unknown).status_code == 404 and world.post(route=unknown).status_code == 404
    )
    assert world.events() == []


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"add": []},
        {"add": ["Upper"]},
        {"add": ["1leading-digit"]},
        {"add": ["x" * 33]},
        {"add": [f"l{i}" for i in range(9)]},
        {"add": ["ok"], "remove": ["ok"]},
        {"add": "cc-by"},
    ],
)
def test_malformed_label_requests_change_nothing(world, body):
    assert world.post(body).status_code in {400, 422}
    assert world.events() == [] and world.get().json()["labelled"] is False


def test_the_label_limit_holds_across_requests(world):
    world.post({"add": [f"l{i}" for i in range(8)]})
    assert world.post({"add": ["one-more"]}).status_code in {400, 409, 422}
    assert len(world.get().json()["labels"]) == 8
