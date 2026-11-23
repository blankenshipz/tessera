"""A pinned ``public_artifact`` with no declared licence stays refused for Salad (fail closed).

Fakes only. The refusal must be a licence-label refusal (``LicenseRefused``), whatever label
wording the resolver uses, and must not depend on the exact ``unknown-lineage`` marker.
"""

from __future__ import annotations

import pytest
from salad_campaign_fixture import (  # noqa: F401
    SaladWorld,
    salad_provider_reset,
    salad_spec_v2,
    salad_stage_v2,
)

from tessera_control.campaign_store import LicenseRefused

PERMISSIVE = {"mit", "apache-2.0", "cc0-1.0", "cc-by-4.0", "bsd-3-clause"}


def public_source(**extra):
    return {
        "kind": "public_artifact",
        "name": "weights",
        "files": [
            {
                "name": "w",
                "url": "https://models.example.com/w.bin",
                "sha256": "c" * 64,
                "max_bytes": 1000,
            }
        ],
    } | extra


def world_with(tmp_path, source):
    world = SaladWorld(tmp_path)
    world.spec = salad_spec_v2(world.env, [salad_stage_v2(world.env, inputs=[source])])
    return world


def test_an_undeclared_licence_pin_is_refused_for_salad(tmp_path):
    world = world_with(tmp_path, public_source())
    with pytest.raises(LicenseRefused) as refused:
        world.env.submit(spec=world.spec)
    labels = {label.lower() for label in refused.value.labels}
    assert labels, "the refusal names the effective labels it refused"
    assert not labels & PERMISSIVE
    assert "licence" in str(refused.value).lower() or "license" in str(refused.value).lower()
    assert "salad" in str(refused.value)


def test_the_refusal_leaves_no_campaign_behind(tmp_path):
    world = world_with(tmp_path, public_source())
    with pytest.raises(LicenseRefused):
        world.env.submit(spec=world.spec)
    assert world.row("SELECT 1 FROM campaigns") is None


@pytest.mark.skip(
    reason="waits for the public_artifact licence-label rule (ADR 0040); not implemented on main"
)
def test_a_pin_with_a_declared_permissive_licence_is_accepted(tmp_path):
    world = world_with(tmp_path, public_source(license="MIT"))
    submitted = world.env.submit(spec=world.spec)
    assert submitted["id"]
