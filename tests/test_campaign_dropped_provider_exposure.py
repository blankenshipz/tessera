"""A provider removed from operator config still counts as open exposure (fail closed)."""

from __future__ import annotations

from campaign_runner_harness import World
from test_campaign_providers import _v2_chain, fake_provider  # noqa: F401

from tessera_control.campaign_providers import unregister_provider


def test_an_unregistered_provider_with_an_unsettled_attempt_blocks_a_new_hold(
    tmp_path,
    fake_provider,  # noqa: F811
):
    world = World(tmp_path)
    env = world.env
    fake_provider("fake-cpu", principal=env.worker["id"])
    env.trust(provider="fake-cpu")
    campaign = world.campaign(_v2_chain(env, "fake-cpu"))
    cid = campaign["id"]
    world.tick_until(lambda: world.attempts(cid), worker=False)
    assert world.attempts(cid)[0]["settled_cents"] is None
    runner = world.runner
    # Build the runner's per-campaign context the way a tick does.
    ctx = runner._load(cid)
    assert ctx is not None
    with runner.store.runner_connection() as connection:
        assert runner._dropped_provider_exposure(connection, ctx, cid) is False
        unregister_provider("fake-cpu")
        assert runner._dropped_provider_exposure(connection, ctx, cid) is True
