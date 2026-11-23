"""The provider-side cache of pinned public files (ADR 0097): admission and receipts. Fakes only.

Admission: a spec that asks for the cache must see it in the reviewed inline manifest, and a
manifest may not enable caching the approved spec did not. Receipts: per file ``cache`` from the
provider's own report (a closed set), ``cache_requested`` only when the spec asked, and no file
bytes anywhere (the receipt stays ``via_control: false``).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import pytest
from salad_campaign_fixture import (  # noqa: F401
    SaladWorld,
    salad_provider_reset,
    salad_spec_v2,
    salad_stage_v2,
)

from tessera_control import campaign_provider_salad as module
from tessera_control.campaign_models import CampaignSpecV2, NormalInput, normalize
from tessera_control.campaign_provider_salad import (
    PUBLIC_ARTIFACT_CACHE,
    PUBLIC_ARTIFACT_CACHE_INLINE,
    public_cache_flags,
)
from tessera_control.campaign_receipts import (
    CACHE_OUTCOMES,
    _local_fetched,
    cache_outcome,
    public_fetch_entry,
)

URL = "https://models.example.com/w.bin"
URL2 = "https://models.example.com/v.bin"
SHA = "c" * 64
SHA2 = "d" * 64
SIZE = 1000
RUNTIME = "gpu_runtime_v1"


def source(*, cache, files=None, name="weights"):
    files = files or ((("w", URL, SHA, SIZE)),)
    return NormalInput(
        "public_artifact",
        name,
        public_files=tuple(files if isinstance(files[0], tuple) else (files,)),
        cache=cache,
    )


def stage_with(*inputs):
    return SimpleNamespace(inputs=tuple(inputs), image="x@sha256:" + "1" * 64)


def config(runtime=RUNTIME):
    rows = [SimpleNamespace(url=URL, sha256=SHA, max_bytes=SIZE)]
    egress = SimpleNamespace(fetches=rows, redirect_hosts=[])
    return SimpleNamespace(workload=SimpleNamespace(egress=egress, input=None, runtime=runtime))


def manifest(*flags):
    """An inline manifest with one URL item per ``(url, sha, size, cache)``; cache None = absent."""
    items = []
    for url, sha, size, cache in flags:
        item = {"url": url, "sha256": sha, "max_bytes": size, "kind": "file", "dest": "w.bin"}
        if cache is not None:
            item["cache"] = cache
        items.append(item)
    items.append({"kind": "entry", "source": "input", "path": "main.py"})
    return {"schema": "tessera.gpu-runtime.manifest.v1", "fetches": items}


@pytest.fixture
def inline(monkeypatch):
    def use(value):
        monkeypatch.setattr(module, "gpu_runtime_inline_manifest", lambda workload: value)

    return use


# -- admission --------------------------------------------------------------------------------


def test_the_cache_flag_per_pin_comes_from_the_spec_source():
    flags = public_cache_flags(
        stage_with(
            source(cache=True), source(cache=False, files=("v", URL2, SHA2, 5), name="other")
        )
    )
    assert flags == {(URL, SHA, SIZE): True, (URL2, SHA2, 5): False}


def test_a_matching_cache_flag_is_accepted(inline):
    inline(manifest((URL, SHA, SIZE, True)))
    assert module._cache_refusal(stage_with(source(cache=True)), config()) is None
    inline(manifest((URL, SHA, SIZE, None)))  # absent means false
    assert module._cache_refusal(stage_with(source(cache=False)), config()) is None
    inline(manifest((URL, SHA, SIZE, False)))
    assert module._cache_refusal(stage_with(source(cache=False)), config()) is None


@pytest.mark.parametrize(
    ("spec_cache", "manifest_flag"),
    [(True, None), (True, False), (False, True)],
)
def test_a_flag_that_differs_either_way_is_refused(inline, spec_cache, manifest_flag):
    inline(manifest((URL, SHA, SIZE, manifest_flag)))
    got = module._cache_refusal(stage_with(source(cache=spec_cache)), config())
    assert got == PUBLIC_ARTIFACT_CACHE


def test_a_non_boolean_flag_is_refused(inline):
    inline(manifest((URL, SHA, SIZE, "yes")))
    assert module._cache_refusal(stage_with(source(cache=True)), config()) == PUBLIC_ARTIFACT_CACHE


def test_a_cached_pin_the_manifest_does_not_carry_is_refused(inline):
    inline(manifest((URL2, SHA2, 5, True)))
    assert module._cache_refusal(stage_with(source(cache=True)), config()) == PUBLIC_ARTIFACT_CACHE


def test_caching_needs_an_inline_manifest(inline):
    inline(None)  # a manifest carried in the input cannot be inspected at plan time
    assert (
        module._cache_refusal(stage_with(source(cache=True)), config())
        == PUBLIC_ARTIFACT_CACHE_INLINE
    )
    assert module._cache_refusal(stage_with(source(cache=False)), config()) is None
    # A workload that is not the gpu_runtime_v1 layout has no manifest to review either.
    assert (
        module._cache_refusal(stage_with(source(cache=True)), config(runtime=None))
        == PUBLIC_ARTIFACT_CACHE_INLINE
    )
    assert module._cache_refusal(stage_with(source(cache=False)), config(runtime=None)) is None


def test_the_full_refusal_carries_the_cache_reason(inline):
    inline(manifest((URL, SHA, SIZE, True)))
    item = stage_with(source(cache=False))
    item.inputs = item.inputs  # a stage with no snapshot: no delivery check applies
    got = module.public_artifact_refusal(item, config())
    assert got == PUBLIC_ARTIFACT_CACHE


def test_a_real_stage_without_cache_still_passes_admission(tmp_path):
    world = SaladWorld(tmp_path)
    module.register_provider_instance(world.profiles)
    spec = CampaignSpecV2.model_validate(salad_spec_v2(world.env, [salad_stage_v2(world.env)]))
    base = normalize(spec).stages[0]
    item = replace(base, inputs=(*base.inputs, source(cache=False)))
    assert module.public_artifact_refusal(item, module.salad_config(item)) is None


# -- receipts ---------------------------------------------------------------------------------


def receipt_stage(cache):
    return SimpleNamespace(inputs=(source(cache=cache),))


def test_provider_reported_cache_outcomes_are_a_closed_set():
    assert CACHE_OUTCOMES == {"hit", "miss", "unavailable", "off"}
    for value in CACHE_OUTCOMES:
        assert cache_outcome({"cache": value}) == value
    for bad in ("HIT", "cached", "", 1, True, None, ["hit"]):
        assert cache_outcome({"cache": bad}) is None
    assert cache_outcome({}) is None


def test_a_local_workers_cached_boolean_maps_to_hit_or_miss():
    assert cache_outcome({"cached": True}) == "hit"
    assert cache_outcome({"cached": False}) == "miss"
    assert cache_outcome({"cached": "yes"}) is None
    assert cache_outcome({"cache": "bogus", "cached": True}) is None  # a bad report is not rescued


@pytest.mark.parametrize("value", sorted(CACHE_OUTCOMES))
def test_a_salad_row_shows_its_outcome_and_bytes(value):
    rows = [{"url": URL, "sha256": SHA, "bytes": 640, "seconds": 1.5, "cache": value}]
    entry = public_fetch_entry("salad", receipt_stage(True), rows)
    (file,) = entry["files"]
    assert file["cache"] == value and file["bytes"] == 640
    assert file["evidence"] == "provider_reported"
    assert entry["via_control"] is False and entry["cache_requested"] is True


def test_without_a_provider_row_the_cache_is_unknown_and_the_file_declared_only():
    entry = public_fetch_entry("salad", receipt_stage(True), None)
    (file,) = entry["files"]
    assert file["cache"] is None and file["bytes"] is None
    assert file["evidence"] == "declared_only"
    assert entry["cache_requested"] is True


def test_cache_requested_appears_only_when_the_spec_asked():
    entry = public_fetch_entry("salad", receipt_stage(False), None)
    assert "cache_requested" not in entry
    assert "cache" not in entry["files"][0]  # nothing asked, nothing reported: shape unchanged
    rows = [{"url": URL, "sha256": SHA, "bytes": 1, "seconds": 1, "cache": "off"}]
    assert public_fetch_entry("salad", receipt_stage(False), rows)["files"][0]["cache"] == "off"


def test_a_row_for_another_digest_is_not_used():
    rows = [{"url": URL, "sha256": SHA2, "bytes": 640, "seconds": 1, "cache": "hit"}]
    (file,) = public_fetch_entry("salad", receipt_stage(True), rows)["files"]
    assert file["cache"] is None and file["evidence"] == "declared_only"


def test_a_stage_without_public_inputs_has_no_public_fetch_entry():
    assert public_fetch_entry("salad", SimpleNamespace(inputs=()), None) is None


def test_the_receipt_carries_no_file_content_or_url():
    rows = [
        {"url": URL, "sha256": SHA, "bytes": 640, "seconds": 1, "cache": "hit", "content": "SECRET"}
    ]
    entry = public_fetch_entry("salad", receipt_stage(True), rows)
    text = json.dumps(entry)
    assert "SECRET" not in text and URL not in text
    assert set(entry["files"][0]) == {
        "name",
        "source_host",
        "sha256",
        "bytes",
        "seconds",
        "evidence",
        "cache",
    }


# -- local worker rows ------------------------------------------------------------------------


def events_db(rows):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE events (event_type TEXT, payload_json TEXT)")
    for payload in rows:
        connection.execute(
            "INSERT INTO events VALUES ('execution.public_fetch_recorded', ?)",
            (json.dumps(payload),),
        )
    return connection


def recorded(**over):
    row = {
        "input": "weights",
        "name": "w",
        "source_host": "models.example.com",
        "sha256": SHA,
        "bytes": 640,
        "seconds": 0.5,
        "cached": True,
    }
    return {"operation_id": "op1", "files": [{**row, **over}], "via_control": False}


def test_the_local_workers_recorded_row_reaches_the_receipt_with_its_cache_outcome():
    stage = receipt_stage(True)
    for cached, expected in ((True, "hit"), (False, "miss")):
        connection = events_db([recorded(cached=cached)])
        # one dict-like attempt, as the receipt builder passes it
        rows = _local_fetched(connection, {"operation_id": "op1"}, stage)
        (file,) = public_fetch_entry("local-docker", stage, rows)["files"]
        assert file["cache"] == expected and file["bytes"] == 640
        assert file["evidence"] == "provider_reported"


def test_a_local_row_that_contradicts_the_pin_or_has_no_event_is_ignored():
    stage = receipt_stage(True)
    attempt = {"operation_id": "op1"}
    assert _local_fetched(events_db([]), attempt, stage) is None
    assert _local_fetched(events_db([recorded(sha256=SHA2)]), attempt, stage) is None
    assert _local_fetched(events_db([recorded(name="other")]), attempt, stage) is None
    assert _local_fetched(events_db([recorded()]), {"operation_id": None}, stage) is None
