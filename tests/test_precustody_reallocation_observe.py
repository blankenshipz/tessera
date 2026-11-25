"""A repeated observe of one Salad attempt answers the same after the runner resolved it."""

from __future__ import annotations

import pytest

from tessera_control.campaign_infra_retry import REALLOCATED_PRECUSTODY
from tessera_control.campaign_provider_salad import SaladRouteProvider


def _attempt(detail, failure_class=None):
    return {
        "salad_verdict": "failed",
        "salad_evidence_digest": "e" * 64,
        "failure_detail": detail,
        "failure_class": failure_class,
        "manifest_id": None,
    }


def _provider():
    return SaladRouteProvider.__new__(SaladRouteProvider)


def test_a_second_observe_after_the_runner_recorded_the_first_agrees():
    provider = _provider()
    row = _attempt(f"{REALLOCATED_PRECUSTODY}: allocation changed; termination confirmed")
    first = provider.observe(None, row)
    # What _do_resolve leaves on the stored row after the first observation.
    row["failure_class"] = first.failure_class
    row["failure_detail"] = first.detail
    second = provider.observe(None, row)
    assert first.outcome == second.outcome == "failed_transient"
    assert first.failure_class == second.failure_class == REALLOCATED_PRECUSTODY
    assert second.detail == first.detail
    assert provider.observe(None, row).detail == first.detail


@pytest.mark.parametrize("detail", ["allocation changed; confirmed", "not passed: x"])
def test_a_permanent_failure_stays_permanent_on_repeat(detail):
    provider = _provider()
    row = _attempt(detail)
    first = provider.observe(None, row)
    row["failure_class"] = first.failure_class
    row["failure_detail"] = first.detail
    second = provider.observe(None, row)
    assert first.outcome == second.outcome == "failed_permanent"
    assert second.failure_class == "salad_not_passed"
    assert second.detail == first.detail
