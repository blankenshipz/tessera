from typing import get_args

import pytest
from pydantic import ValidationError

from tessera_control.agent_authority_models import ConsumeAgentAuthority, DispatchAgentAttempt
from tessera_mcp.dispatch_profiles import DISPATCH_PROFILES
from tessera_mcp.mcp_workflow import Consume, Dispatch


def payload(profile):
    return {
        "expected_revision": 1,
        "expected_epoch": 1,
        "model": "attested-model",
        "command": {
            "action": "attempt:dispatch",
            "attempt_id": "attempt",
            "expected_revision": 1,
            "target_digest": "1" * 64,
            "profile_version": profile,
        },
    }


def test_adapter_profiles_are_exactly_the_control_contract():
    control = get_args(DispatchAgentAttempt.model_fields["profile_version"].annotation)
    assert DISPATCH_PROFILES == control
    assert get_args(Dispatch.model_fields["profile_version"].annotation) == control


@pytest.mark.parametrize("profile", ["development-minikube-opencode-v1", "ordinary-docker-v1"])
def test_dev_and_cpu_profiles_round_trip_to_the_control_model(profile):
    body = Consume.model_validate(payload(profile)).model_dump(mode="json")
    assert body == payload(profile)
    assert ConsumeAgentAuthority.model_validate(body).model_dump(mode="json") == body


@pytest.mark.parametrize(
    "profile", ["docker-local-cpu-v1", "kubernetes-job-v1", "salad-container-v1", ""]
)
def test_profiles_outside_the_control_contract_are_refused(profile):
    with pytest.raises(ValidationError):
        Consume.model_validate(payload(profile))
