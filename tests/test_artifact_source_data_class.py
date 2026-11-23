"""An artifact input may state its data class; it can never be less restrictive (ADR 0039)."""

from __future__ import annotations

import pytest
from campaign_runner_harness import World
from pydantic import ValidationError
from test_campaign_io import _finished, _project, _source

from tessera_control.campaign_io import resolve_artifact
from tessera_control.campaign_models import ArtifactSource

BASE = {
    "kind": "artifact",
    "name": "pinned",
    "campaign_id": "campaign-1",
    "stage": "s1",
    "output": "out",
    "sha256": "a" * 64,
}


def test_default_class_is_private_derived_and_not_serialized():
    source = ArtifactSource.model_validate(BASE)
    assert source.data_class == "private_derived"
    assert "data_class" not in source.model_dump(mode="json")


def test_explicit_private_derived_is_accepted_and_kept_in_the_spec():
    source = ArtifactSource.model_validate({**BASE, "data_class": "private_derived"})
    assert source.model_dump(mode="json")["data_class"] == "private_derived"


@pytest.mark.parametrize("value", ["public", "internal", "", None, 3])
def test_a_downgrade_or_unknown_class_is_refused(value):
    with pytest.raises(ValidationError):
        ArtifactSource.model_validate({**BASE, "data_class": value})


def test_resolved_artifact_and_receipt_input_row_carry_the_restrictive_class(tmp_path):
    world = World(tmp_path)
    campaign, row = _finished(world)
    with world.runner.store.runner_connection() as connection:
        declared = resolve_artifact(
            connection, _project(world), _source(campaign, row, data_class="private_derived")
        )
        plain = resolve_artifact(connection, _project(world), _source(campaign, row))
    assert declared.data_class == plain.data_class == "private_derived"
