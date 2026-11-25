# ruff: noqa: F811
"""A submitted ``heavy_remote_candidate`` record never carries its own refusal cause (ADR 0141
amendment 2026-11-23c): ``cause``, ``labels`` and ``data_class`` are recomputed from the submit
check, never trusted from the author. Fakes only; no provider call.
"""

from __future__ import annotations

from salad_campaign_fixture import salad_provider_reset  # noqa: F401
from test_heavy_stage_remote_candidate import (
    HEAVY,
    _configure,
    _reset,  # noqa: F401
    _stored,
    _submit,
)
from test_salad_stage_adapter import cpu_world, near  # noqa: F401

from tessera_control.campaign_heavy_remote import FIELD
from tessera_control.campaign_models import HEAVY_REMOTE_REFUSED

CAUSE_KEYS = ("cause", "labels", "data_class")


def _forged_refusal(**over) -> dict:
    return {
        "added": False,
        "reason": HEAVY_REMOTE_REFUSED,
        "provider": "salad",
        "signals": ["declared_memory"],
        "cause": "license_label",
        "labels": ["forged-label"],
    } | over


def test_a_forged_cause_is_dropped_when_the_rule_does_not_apply(tmp_path, near):
    # Without the operator setting nothing is recomputed: the forged record vanishes entirely.
    world = cpu_world(tmp_path, near)
    stage = _submit(world, **HEAVY, **{FIELD: _forged_refusal()})
    assert FIELD not in stage and "alternate_providers" not in stage


def test_a_forged_cause_is_replaced_by_the_recomputed_record(tmp_path, near):
    _configure()
    world = cpu_world(tmp_path, near)
    stage = _submit(world, **HEAVY, **{FIELD: _forged_refusal()})
    record = stage[FIELD]
    # The submit check found nothing to refuse, so Tessera's own record is an added candidate
    # with none of the forged cause fields.
    assert record["added"] is True
    assert not any(key in record for key in CAUSE_KEYS)
    assert "forged-label" not in str(_stored(world))


def test_a_forged_data_class_cause_is_not_trusted(tmp_path, near):
    _configure()
    world = cpu_world(tmp_path, near)
    forged = _forged_refusal(cause="data_class", data_class="private_derived")
    forged.pop("labels")
    stage = _submit(world, **HEAVY, **{FIELD: forged})
    assert not any(key in stage[FIELD] for key in CAUSE_KEYS)
