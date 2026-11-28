"""ADR 0216: provider outcome x guardian final x output kept, walked end to end.

Each cell runs one claimed Salad attempt to its outcome in the fake world, lets the runner work
(with the clock moved past every wait bound, the charge deadline included) and reads where the
attempt came to rest. Every cell must reach a settled attempt with no person's reconcile, or be
on ``HUMAN``: the explicit list of cells that still need a person. ``HUMAN`` may only shrink: a
cell on it that starts settling on its own fails here until it is taken off, and the list's size
is pinned.

Fakes only (the Salad campaign world, its guardian reader and executor doubles; no network).
"""

from __future__ import annotations

import hashlib
import itertools
from datetime import UTC, datetime, timedelta

import pytest
from salad_campaign_fixture import SaladWorld, salad_provider_reset, salad_stage  # noqa: F401

from tessera_control.salad_guardian import RECONCILE_OPERATOR, RECONCILE_PROVIDER
from tessera_control.salad_guardian_bound import CHARGE_DEADLINE_SECONDS

STAMP = datetime.now(UTC)
_BASE = dict(
    orphan=False,
    allocation_uncertain=False,
    escalation_pending=False,
    budget_fenced=False,
    rate_unknown=False,
    published_hourly_rate_usd="0.5",
    estimated_exposure_cents="3.2",
    actual_billed_cost_cents=None,
    observed_at=STAMP.isoformat(),
)
FINALS = {
    "stopped_clean": dict(_BASE, status="stopped", reason="cost_cap"),
    "stopped_fenced": dict(
        _BASE,
        status="stopped",
        reason="rate_unknown",
        budget_fenced=True,
        rate_unknown=True,
        published_hourly_rate_usd=None,
        estimated_exposure_cents=None,
    ),
    "reconciled_provider": dict(
        _BASE, status="reconciled", reason=RECONCILE_PROVIDER, allocation_uncertain=True
    ),
    "reconciled_operator": dict(
        _BASE, status="reconciled", reason=RECONCILE_OPERATOR, allocation_uncertain=True
    ),
    "none": None,
}
PROVIDER = ("confirmed", "unconfirmed")
OUTPUT = ("kept", "none")

# Cells that still need a person (provider, guardian final, output). May only shrink.
HUMAN = frozenset(
    {
        # The route never confirmed termination and the guardian has no provider-confirmed
        # close: nothing control holds proves the spend ended (ADR 0212 limits).
        ("unconfirmed", "stopped_clean", "kept"),
        ("unconfirmed", "stopped_clean", "none"),
        ("unconfirmed", "stopped_fenced", "kept"),
        ("unconfirmed", "stopped_fenced", "none"),
        ("unconfirmed", "reconciled_operator", "kept"),
        ("unconfirmed", "reconciled_operator", "none"),
        ("unconfirmed", "none", "kept"),
        ("unconfirmed", "none", "none"),
        # A provider-confirmed close of an uncertain allocation whose output was kept: the
        # output is never accepted and the cost is unbounded by evidence of no work.
        ("unconfirmed", "reconciled_provider", "kept"),
        # A confirmed stop with no guardian final, or only an operator's attested reconcile:
        # past the charge deadline the campaign blocks once and a recorded charge resumes it.
        ("confirmed", "none", "kept"),
        ("confirmed", "none", "none"),
        ("confirmed", "reconciled_operator", "kept"),
        ("confirmed", "reconciled_operator", "none"),
    }
)
HUMAN_CELLS = 13
CELLS = list(itertools.product(PROVIDER, FINALS, OUTPUT))


def _run(world: SaladWorld, provider: str, final: dict | None, output: str) -> None:
    world.to_route_started()
    row = world.salad()
    world.env.store.mark_salad_executing(world.worker, row["attempt_id"], world.key("executing"))
    world.claim("create")
    world.claim("start")
    manifest = world.upload_manifest() if output == "kept" else None
    if provider == "confirmed":
        world.terminate_route()
        world.observe_exit()
    receipt = {
        "schema": "tessera.salad.live-check.v1",
        "status": "passed" if output == "kept" else "inconclusive",
        "reason": "verified_journey" if output == "kept" else "execution_or_output_unresolved",
        "stop_confirmed": provider == "confirmed",
        "custody_work_begun": True,
    }
    if output == "kept":
        content = b'{"score": 1}'  # what the world's manifest upload holds
        receipt["outputs"] = [
            {
                "name": "output.json",
                "sha256": hashlib.sha256(content).hexdigest(),
                "bytes": len(content),
            }
        ]
    world.env.store.record_salad_outcome(
        world.worker,
        row["attempt_id"],
        world.key("outcome"),
        dict(
            route_id=row["route_id"],
            live_receipt=receipt,
            manifest_id=None if manifest is None else manifest["id"],
            provider_id="provider-group-1",
            **({} if final is None else {"guardian_final": final}),
        ),
    )


def _rest(world: SaladWorld) -> str:
    """Tick until the first attempt settles or the campaign blocks on it, past every bound."""
    first = "SELECT * FROM campaign_stage_attempts WHERE attempt=1"
    for later in (0, CHARGE_DEADLINE_SECONDS + 60, 2 * CHARGE_DEADLINE_SECONDS + 120):
        world.runner.now = lambda later=later: datetime.now(UTC) + timedelta(seconds=later)
        for _ in range(30):
            world.tick()
            attempt = world.row(first)
            if attempt["settled_cents"] is not None:
                return "settled"
        if world.status() == "blocked" and world.row(first)["status"] == "unknown":
            continue
    attempt = world.row(first)
    assert attempt["status"] == "unknown" and world.status() == "blocked", world.dump()
    return "human"


def test_the_human_list_only_shrinks():
    assert len(HUMAN) == HUMAN_CELLS
    assert HUMAN <= set(CELLS)


@pytest.mark.parametrize("cell", CELLS, ids=["-".join(cell) for cell in CELLS])
def test_each_cell_settles_without_a_person_or_is_on_the_human_list(tmp_path, cell):
    provider, final, output = cell
    stage = lambda env: salad_stage(env, retry={"max_attempts": 1, "infra_retries": 1})  # noqa: E731
    world = SaladWorld(tmp_path, stages=lambda env: [stage(env)])
    _run(world, provider, FINALS[final], output)
    rest = _rest(world)
    expected = "human" if cell in HUMAN else "settled"
    assert rest == expected, f"{cell}: {rest} (expected {expected})\n{world.dump()}"
    # No cell ever used a person's reconcile route here.
    assert world.events("execution.grant_reconciled_retryable_infra") == []
