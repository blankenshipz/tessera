"""Carried seal names stay distinct and Key-safe when names share a long prefix."""

from __future__ import annotations

import re

from test_campaign_seals import _finished, _from, _parse, _snapshot, _spec, _stage

from tessera_control.campaign_models import normalize
from tessera_control.campaign_rerun import build_retry_spec
from tessera_control.campaign_seals import carried_seals

RAW = "a" * 64
SHARED = "x" * 26
KEY = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


def _plan():
    split = _stage("split", [_snapshot("raw", RAW)], outputs=("test",))
    score = _stage(
        "score", [_from("test", "split", "test")], roles=["evaluation", "audit"]
    )
    seals = [
        {"name": SHARED + "-one", "role": "evaluation", "stage": "split", "output": "test"},
        {"name": SHARED + "-two", "role": "audit", "stage": "split", "output": "test"},
    ]
    spec = _spec(split, score, seals=seals)
    _parse(spec)
    campaign = _finished(spec, {"split": "succeeded", "score": "failed"})
    outputs = {
        "outputs": [
            {"stage_key": "split", "name": "test", "sha256": "2" * 64, "custody_released": True}
        ]
    }
    return spec, campaign, outputs


def test_prefix_sharing_seals_get_distinct_names_and_the_retry_validates():
    _, campaign, outputs = _plan()
    retry = build_retry_spec(campaign, outputs)["spec"]
    seals = retry["seals"]
    assert {(s["role"], s["digest"]) for s in seals} == {
        ("evaluation", "2" * 64),
        ("audit", "2" * 64),
    }
    names = [s["name"] for s in seals]
    assert len(set(names)) == 2
    normalize(_parse(retry))


def test_names_are_deterministic_and_key_safe():
    spec, campaign, outputs = _plan()
    first = build_retry_spec(campaign, outputs)["spec"]["seals"]
    second = build_retry_spec(campaign, outputs)["spec"]["seals"]
    assert first == second
    assert all(KEY.match(s["name"]) for s in first)
    reused = [{"stage": "split", "output": "test", "sha256": "2" * 64}]
    again = carried_seals(normalize(_parse(spec)), {"score"}, reused)
    assert [s["name"] for s in again] == [s["name"] for s in first]


def test_name_depends_on_sha():
    spec, _, _ = _plan()
    normal = normalize(_parse(spec))
    one = carried_seals(normal, {"score"}, [{"stage": "split", "output": "test", "sha256": "2" * 64}])
    two = carried_seals(normal, {"score"}, [{"stage": "split", "output": "test", "sha256": "3" * 64}])
    assert {s["name"] for s in one}.isdisjoint(s["name"] for s in two)
