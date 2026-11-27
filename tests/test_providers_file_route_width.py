"""The ``--campaign-providers`` file accepts ``salad_max_concurrent_routes`` (ADR 0208).

Regression: the field was read by the Salad provider module but ``ProvidersFile`` (closed,
``extra="forbid"``) did not declare it, so ``tessera serve`` refused a file that set it.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tessera_control.campaign_provider_salad import register_from_config as salad_register
from tessera_control.campaign_provider_worker import ProvidersFile, config_sections

SCHEMA = "tessera.campaign-providers.v1"


def _document(**extra):
    return {"schema": SCHEMA, **extra}


def test_a_recorded_route_width_is_accepted():
    parsed = ProvidersFile.model_validate(_document(salad_max_concurrent_routes=2))
    assert parsed.salad_max_concurrent_routes == 2


@pytest.mark.parametrize("width", [1, 4])
def test_the_route_width_bounds_are_inclusive(width):
    parsed = ProvidersFile.model_validate(_document(salad_max_concurrent_routes=width))
    assert parsed.salad_max_concurrent_routes == width


def test_an_unset_route_width_stays_unbounded():
    assert ProvidersFile.model_validate(_document()).salad_max_concurrent_routes is None
    parsed = ProvidersFile.model_validate(_document(salad_max_concurrent_routes=None))
    assert parsed.salad_max_concurrent_routes is None


def test_the_worker_section_validator_accepts_the_route_width():
    for section in config_sections():
        section.validate(_document(salad_max_concurrent_routes=2))


@pytest.mark.parametrize("bad", [True, False, 0, 5, -1, "2", 2.0])
def test_a_route_width_the_salad_module_refuses_is_refused_by_the_file(bad):
    with pytest.raises(ValidationError, match="salad_max_concurrent_routes"):
        ProvidersFile.model_validate(_document(salad_max_concurrent_routes=bad))
    # The Salad module refuses the same value, so both readers agree.
    with pytest.raises(ValueError, match="salad_max_concurrent_routes"):
        salad_register({"salad_max_concurrent_routes": bad})
