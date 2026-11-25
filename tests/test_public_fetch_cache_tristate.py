"""The provider-fetch cache outcome is tri-state on receipts: hit, miss, unavailable.

A recorded fetch row may carry an optional ``cache`` string next to the ``cached`` boolean.
``unavailable`` reads as itself (not unknown); unknown strings never become trusted outcomes;
rows without the field keep their exact shape. Fakes only.
"""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from tessera_control.campaign_models import NormalInput
from tessera_control.campaign_receipts import (
    _local_fetched,
    cache_outcome,
    public_fetch_entry,
)
from tessera_control.public_fetch_models import PublicFetchRow, ReportPublicFetch

URL = "https://models.example.com/w.bin"
SHA = "c" * 64


def stage(cache):
    source = NormalInput(
        "public_artifact", "weights", public_files=(("w", URL, SHA, 1000),), cache=cache
    )
    return SimpleNamespace(inputs=(source,))


def db(files):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE events (event_type TEXT, payload_json TEXT)")
    payload = {"operation_id": "op1", "files": files, "via_control": False}
    connection.execute(
        "INSERT INTO events VALUES ('execution.public_fetch_recorded', ?)", (json.dumps(payload),)
    )
    return connection


def row(**over):
    base = {
        "input": "weights",
        "name": "w",
        "source_host": "models.example.com",
        "sha256": SHA,
        "bytes": 640,
        "seconds": 0.5,
    }
    return {**base, **over}


def receipt_cache(files, requested=True):
    st = stage(requested)
    fetched = _local_fetched(db(files), {"operation_id": "op1"}, st)
    (file,) = public_fetch_entry("local-docker", st, fetched)["files"]
    return file["cache"]


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        ({"cache": "hit", "cached": True}, "hit"),
        ({"cache": "miss", "cached": False}, "miss"),
        ({"cache": "unavailable"}, "unavailable"),
        ({"cached": True}, "hit"),
        ({"cached": False}, "miss"),
        ({"cache": "unavailable", "cached": None}, "unavailable"),
    ],
)
def test_recorded_rows_read_as_their_tri_state(item, expected):
    assert receipt_cache([row(**item)]) == expected


@pytest.mark.parametrize("bad", ["HIT", "cached", "", 1, True, ["hit"], None])
def test_an_unknown_recorded_cache_value_reads_as_unknown_never_trusted(bad):
    # Even with a boolean beside it: a present but invalid ``cache`` is not trusted.
    assert receipt_cache([row(cache=bad, cached=True)]) is None
    if bad != "off":
        assert cache_outcome({"cache": bad, "cached": True}) is None


def test_a_row_without_the_field_keeps_its_exact_shape():
    st = stage(False)
    fetched = _local_fetched(db([row(cached=True)]), {"operation_id": "op1"}, st)
    assert fetched == [{"url": URL, "sha256": SHA, "bytes": 640, "seconds": 0.5, "cached": True}]


def test_a_spec_that_never_asked_and_a_silent_row_has_no_cache_key():
    st = stage(False)
    (file,) = public_fetch_entry("p", st, None)["files"]
    assert "cache" not in file


def test_the_report_model_accepts_the_tri_state_and_rejects_contradictions():
    assert PublicFetchRow(**row(cached=True)).cache is None
    assert PublicFetchRow(**row(cache="hit", cached=True)).cache == "hit"
    assert PublicFetchRow(**row(cache="miss", cached=False)).cache == "miss"
    unavailable = PublicFetchRow(**row(cache="unavailable"))
    assert unavailable.cached is None
    for bad in (
        row(),  # neither field
        row(cache="hit", cached=False),
        row(cache="miss", cached=True),
        row(cache="unavailable", cached=False),
        row(cache="off", cached=False),
        row(cache="HIT", cached=True),
        row(cached="yes"),
    ):
        with pytest.raises(ValidationError):
            PublicFetchRow(**bad)
    with pytest.raises(ValidationError):
        ReportPublicFetch(lease_id="l", expected_epoch=1, files=[])


def test_the_recorded_event_stores_only_present_cache_keys():
    from tessera_control.public_fetch_report import PublicFetchStore  # noqa: F401

    dumped = ReportPublicFetch(
        lease_id="l",
        expected_epoch=1,
        files=[row(cached=True), row(name="v", cache="unavailable")],
    ).model_dump(mode="json")["files"]
    stored = [
        {k: v for k, v in r.items() if k not in ("cache", "cached") or v is not None}
        for r in dumped
    ]
    assert "cache" not in stored[0] and stored[0]["cached"] is True
    assert stored[1]["cache"] == "unavailable" and "cached" not in stored[1]
