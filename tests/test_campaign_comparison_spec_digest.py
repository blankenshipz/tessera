"""The comparison verifies the stored spec against its digest on read.

No edit made after approval is read."""

from __future__ import annotations

import json

import pytest
from test_campaign_comparison import (  # noqa: F401  (web is a fixture)
    QUERY,
    _connect,
    _good,
    _write_receipt,
    web,
)

from tessera_control.campaign_comparison import ComparisonRefused
from tessera_control.campaign_models import parse_spec_json, spec_digest
from tessera_control.campaign_store import _verified_spec_stages


def _get(client, headers, cid):
    return client.get(f"/campaigns/{cid}/comparison?{QUERY}", headers=headers)


def _edit_spec(database, cid, edit):
    with _connect(database) as connection:
        text = connection.execute(
            "SELECT spec_json FROM campaigns WHERE id=?", (cid,)
        ).fetchone()["spec_json"]
        spec = json.loads(text)
        edit(spec)
        connection.execute(
            "UPDATE campaigns SET spec_json=? WHERE id=?", (json.dumps(spec, sort_keys=True), cid)
        )


def test_an_untampered_spec_is_read_as_before(web):  # noqa: F811
    client, headers, cid, database = web
    _write_receipt(database, cid, _good())
    assert _get(client, headers, cid).status_code == 200


@pytest.mark.parametrize(
    "edit",
    [
        lambda spec: spec.update(name="renamed after approval"),
        lambda spec: spec.update(budget_cap_cents=spec["budget_cap_cents"] - 1),
        lambda spec: spec["stages"][0].update(argv=["python", "-c", "print(2)"]),
    ],
    ids=["name", "budget", "argv"],
)
def test_a_spec_edited_after_approval_is_refused_by_name(web, edit):  # noqa: F811
    client, headers, cid, database = web
    _write_receipt(database, cid, _good())
    assert _get(client, headers, cid).status_code == 200
    _edit_spec(database, cid, edit)
    response = _get(client, headers, cid)
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "spec_digest_mismatch" in detail and "split_lock_differs" not in detail


def test_a_tampered_declaration_that_invalidates_the_spec_is_unavailable_not_read(web):  # noqa: F811
    # A bare gate_evidence split lock is not a valid stage, so the edited text cannot be parsed
    # and therefore cannot be verified: refused as spec_unavailable, never read for its arms.
    client, headers, cid, database = web
    _write_receipt(database, cid, _good())
    _edit_spec(
        database,
        cid,
        lambda spec: spec["stages"][0].update(gate_evidence={"split_lock_id": "tampered"}),
    )
    response = _get(client, headers, cid)
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "spec_unavailable" in detail and "split_lock_differs" not in detail, detail


def test_a_stored_spec_that_cannot_be_read_is_unavailable_not_trusted(web):  # noqa: F811
    client, headers, cid, database = web
    _write_receipt(database, cid, _good())
    with _connect(database) as connection:
        connection.execute("UPDATE campaigns SET spec_json=? WHERE id=?", ("{not json", cid))
    response = _get(client, headers, cid)
    assert response.status_code == 409
    assert "spec_unavailable" in response.json()["detail"]


def test_verified_stages_come_from_the_parsed_model_and_digest_is_recomputed(web):  # noqa: F811
    _client, _headers, cid, database = web
    with _connect(database) as connection:
        row = connection.execute(
            "SELECT spec_json, spec_digest FROM campaigns WHERE id=?", (cid,)
        ).fetchone()
    parsed = parse_spec_json(row["spec_json"])
    assert spec_digest(parsed) == row["spec_digest"]
    stages = _verified_spec_stages(row["spec_json"], row["spec_digest"])
    assert stages == parsed.model_dump(mode="json")["stages"]
    with pytest.raises(ComparisonRefused) as raised:
        _verified_spec_stages(row["spec_json"], "0" * 64)
    assert raised.value.code == "spec_digest_mismatch"
    assert _verified_spec_stages("{not json", row["spec_digest"]) is None
