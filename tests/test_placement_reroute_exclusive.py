"""One way to move a stage between providers: placement alternates or an infrastructure reroute.

ADR 0060 places a stage on its approved candidates (and re-places it for every retry, an
infrastructure retry included); ADR 0058 reroutes an infrastructure retry along its own list.
A stage that names both would have two owners for its provider, so the spec is refused at
submit until the two share one candidate list (the ADR 0060 follow-up).
"""

from __future__ import annotations

import pytest
from test_campaign_models_v2 import _spec2
from test_campaign_placement import FAR, _stage, _world, providers  # noqa: F401

from tessera_control.store import StoreError, canonical_digest


def _submit(env, spec):
    body = {"spec": spec}
    digest = canonical_digest(body)
    status, result = env.store.submit_campaign(
        env.owner, env.project["id"], "k-" + digest[:12], digest, body
    )
    assert status == 201
    return result


def test_a_stage_cannot_name_alternates_and_a_reroute_list(tmp_path, providers):  # noqa: F811
    world = _world(tmp_path, providers)
    env = world.env
    both = _stage(
        env, "a", retry={"max_attempts": 1, "infra_retries": 1, "reroute_providers": [FAR]}
    )
    with pytest.raises(StoreError, match="not both"):
        _submit(env, _spec2(both))


def test_either_one_alone_is_admitted(tmp_path, providers):  # noqa: F811
    world = _world(tmp_path, providers)
    env = world.env
    assert _submit(env, _spec2(_stage(env, "a")))["id"]  # alternates only
    reroute = _stage(
        env,
        "b",
        alternates=(),
        retry={"max_attempts": 1, "infra_retries": 1, "reroute_providers": [FAR]},
    )
    assert _submit(env, _spec2(reroute))["id"]  # a reroute list only
