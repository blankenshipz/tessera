"""Sealed-holdout guard for model inputs (ADR 0068). Synthetic digests only."""

from __future__ import annotations

import pytest
from test_campaign_holdout import _labels, _spec, _world

from tessera_agent.model_input_guard import SealedInputRefused, refuse_sealed_inputs
from tessera_control import campaign_provider_worker as worker
from tessera_control.agent_campaign_reads import AgentCampaignReads

A, B = "a" * 64, "b" * 64


def _body(claims=(), truncated=False) -> dict:
    return {"project_id": "p", "claims": list(claims), "truncated": truncated, "boundary": "x"}


def _code(digests, lookup) -> str:
    with pytest.raises(SealedInputRefused) as info:
        refuse_sealed_inputs(digests, lookup)
    return str(info.value)


def test_no_digests_means_no_lookup():
    def lookup(_):
        raise AssertionError("must not be called")

    refuse_sealed_inputs([], lookup)
    refuse_sealed_inputs(iter(()), lookup)


def test_no_claims_passes_and_looks_up_each_digest_once():
    seen: list[str] = []

    def lookup(d):
        seen.append(d)
        return _body()

    refuse_sealed_inputs([A, B, A], lookup)
    assert seen == [A, B]


def test_a_claim_on_any_digest_is_sealed():
    lookup = lambda d: _body([{"id": "c"}] if d == B else [])  # noqa: E731
    assert _code([A, B], lookup) == "model_input_sealed"


@pytest.mark.parametrize(
    "result",
    [
        _body(truncated=True),
        {"claims": []},
        {"truncated": False},
        {"claims": "x", "truncated": False},
        {"claims": [], "truncated": "no"},
        None,
        [],
    ],
)
def test_malformed_or_truncated_fails_closed(result):
    assert _code([A], lambda d: result) == "model_input_seal_unknown"


def test_lookup_failure_fails_closed_without_leaking():
    def lookup(d):
        raise RuntimeError(f"boom {d} secret-body")

    assert _code([A], lookup) == "model_input_seal_unknown"


@pytest.mark.parametrize("bad", ["", "xyz", "A" * 64, "a" * 63, "a" * 65, None, 5])
def test_invalid_digest_is_refused_before_any_lookup(bad):
    def lookup(_):
        raise AssertionError("must not be called")

    assert _code([A, bad], lookup) == "model_input_digest_invalid"


def test_errors_never_carry_digests_or_bodies():
    marker = "LEAK-MARKER"
    body = _body([{"id": marker, "partition_digest": A}])
    for lookup, digests in (
        (lambda d: body, [A]),
        (lambda d: _body(truncated=True), [A, B]),
        (lambda d: (_ for _ in ()).throw(RuntimeError(marker + d)), [A]),
    ):
        with pytest.raises(SealedInputRefused) as info:
            refuse_sealed_inputs(digests, lookup)
        text = str(info.value)
        assert text.startswith("model_input_") and marker not in text
        assert A not in text and B not in text
        assert info.value.__cause__ is None
        assert info.value.__context__ is None or info.value.__suppress_context__


def test_real_control_holdout_claims_seal_the_partition_digest(tmp_path):
    try:
        world = _world(tmp_path)
        env = world.env
        labels = _labels(world)
        campaign = world.campaign(_spec(env, labels))
        world.tick_all(campaign["id"])
        assert world.status(campaign["id"]) == "succeeded"
        reads = AgentCampaignReads(env.state)

        def lookup(d):
            return reads.holdout_claims(env.owner, env.project["id"], d)

        assert _code([labels[1]], lookup) == "model_input_sealed"
        refuse_sealed_inputs([env.snapshot_digest, "e" * 64], lookup)
    finally:
        worker.register_from_config(None)
