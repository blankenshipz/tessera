"""Canonical changed-boundary ledger: declarations never become admission proof."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from tessera_control.admin_models import Configuration, SaladSetupView
from tessera_control.salad_billing import BillingPolicy, billing_projection
from tessera_operator.salad import SaladConfiguration


@pytest.mark.parametrize(
    "field",
    ["ceiling_cents", "prior_cost_cents", "reserved_cents", "ancillary_cents", "shutdown_cents"],
)
@pytest.mark.parametrize("value", [-1, True, 5001, "100", 1.5])
def test_invalid_bounds_are_not_coerced(field, value):
    with pytest.raises(ValidationError):
        BillingPolicy.model_validate(
            {"mode": "aggregate_capped", "ceiling_cents": 5000, field: value}
        )


def test_cap_and_legacy_modes_do_not_widen_authority():
    for policy in [
        {"mode": "aggregate_capped"},
        {"mode": "aggregate_capped", "ceiling_cents": 0},
        {"ceiling_cents": 1},
        {"prior_cost_cents": 0},
        {
            "mode": "aggregate_capped",
            "ceiling_cents": 5000,
            "prior_cost_cents": 4000,
            "reserved_cents": 1001,
        },
        {"ledger_verified": True},
    ]:
        with pytest.raises(ValidationError):
            BillingPolicy.model_validate(policy)
    assert billing_projection(None)["billing"] == "no_cash_hard_cap_unverified"
    policy = BillingPolicy(
        mode="aggregate_capped", ceiling_cents=5000, ancillary_cents=500, shutdown_cents=1000
    )
    view = billing_projection(policy)
    assert view["billing"] == "aggregate_cap_unverified"
    assert view["billing_declaration"]["accounting"] == "unknown"
    assert view["billing_declaration"]["ledger_verified"] is False
    assert view["billing_declaration"]["enforcement_verified"] is False
    assert view["billing_declaration"]["execution_enabled"] is False


def test_declared_known_and_expired_remain_unverified():
    now = datetime.now(UTC)
    policy = BillingPolicy(
        mode="aggregate_capped",
        ceiling_cents=5000,
        prior_cost_cents=0,
        reserved_cents=0,
        ancillary_cents=500,
        shutdown_cents=1000,
        expires_at=now + timedelta(seconds=1),
    )
    view = billing_projection(policy, now=now)
    assert view["billing_declaration"]["accounting"] == "declared_unverified"
    assert view["billing_declaration"]["lifetime"] == "current"
    assert (
        billing_projection(policy, now=now + timedelta(seconds=2))["billing"]
        == "aggregate_cap_expired"
    )
    assert view["billing_declaration"]["execution_enabled"] is False


def test_legacy_omission_roundtrips_without_new_json_fields():
    local = SaladConfiguration(
        organization="example-org", credential_reference="credential-ref:test", credential_version=1
    )
    assert "billing_policy" not in local.model_dump()
    assert (
        local.model_dump_json()
        == '{"schema_version":1,"integration":"salad","organization":"example-org","project":null,'
        '"proposed_project":"tessera-research","management_host":null,'
        '"credential_reference":"credential-ref:test","credential_version":1}'
    )
    assert (
        SaladConfiguration.model_validate_json(local.model_dump_json()).model_dump_json()
        == local.model_dump_json()
    )
    control = Configuration(component="execution_target", provider="salad")
    assert "billing_policy" not in control.model_dump()
    with pytest.raises(ValidationError):
        Configuration(
            component="control_plane",
            billing_policy={"mode": "aggregate_capped", "ceiling_cents": 5000},
        )
    assert SaladSetupView.model_fields["execution_enabled"].annotation.__args__ == (False,)
