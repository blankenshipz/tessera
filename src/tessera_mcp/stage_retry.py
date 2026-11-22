"""Build a retry spec for the stages of a finished campaign that did not succeed (ADR 0055).

Pure and offline: it reads a campaign (its spec and stage statuses) and its recorded outputs and
returns a NEW v2 spec. It submits nothing and grants nothing: the agent validates and submits the
result like any spec, and a human approves its digest. Stages that already succeeded are not run
again; a retried stage that consumed one of their outputs gets a ``kind: artifact`` input pinning
that output by campaign id and sha256, which the platform only resolves from a succeeded attempt
in a terminal campaign's receipt whose output manifest is retained and custody-released (the
runner does that when the stage succeeds; the ``custody_released`` flag on each recorded output
shows it, and a human's revocation makes the pin fail). Nothing is invented: an output with no
recorded digest, an ambiguous digest, an output that is not custody-released, or a v1 spec makes
the builder refuse instead of guess.
"""

from __future__ import annotations

import copy
from typing import Any

FINISHED = frozenset({"failed", "stopped", "cancelled"})
MAX_NAME = 200


class RetryRefused(ValueError):
    """The campaign cannot be turned into a retry spec; the message says why."""


def _dependencies(stage: dict[str, Any]) -> list[str]:
    return [i["stage"] for i in stage["inputs"] if i.get("kind") == "stage"]


def build_retry_spec(
    campaign: dict[str, Any],
    outputs: dict[str, Any],
    stage_keys: list[str] | None = None,
    budget_cap_cents: int | None = None,
) -> dict[str, Any]:
    """The retry spec and a plain account of what it reuses and re-runs."""
    if campaign.get("status") not in FINISHED:
        raise RetryRefused("Only a failed, stopped or cancelled campaign can be retried.")
    spec = copy.deepcopy(campaign["spec"])
    if spec.get("version") != 2:
        raise RetryRefused("Only a v2 spec can pin earlier outputs; resubmit a v1 spec by hand.")
    status = {row["stage_key"]: row["status"] for row in campaign["stages"]}
    stages = {stage["key"]: stage for stage in spec["stages"]}
    if set(status) != set(stages):
        raise RetryRefused("The campaign's stage records do not match its spec.")
    if stage_keys is not None:
        unknown = sorted(set(stage_keys) - set(stages))
        if unknown:
            raise RetryRefused(f"Unknown stage keys: {unknown}")
        chosen = set(stage_keys)
    else:
        chosen = {key for key, state in status.items() if state != "succeeded"}
    if not chosen:
        raise RetryRefused("Every stage succeeded; there is nothing to retry.")
    rerun = set(chosen)
    changed = True
    while changed:  # a stage that consumed a re-run stage's output must run again too
        changed = False
        for key, stage in stages.items():
            if key not in rerun and any(dep in rerun for dep in _dependencies(stage)):
                rerun.add(key)
                changed = True
    digests: dict[tuple[str, str], set[str]] = {}
    unreleased: set[tuple[str, str]] = set()
    for row in outputs.get("outputs", []):
        digests.setdefault((row["stage_key"], row["name"]), set()).add(row["sha256"])
        if row.get("custody_released") is False:
            unreleased.add((row["stage_key"], row["name"]))
    reused: list[dict[str, str]] = []
    kept = []
    for stage in spec["stages"]:
        if stage["key"] not in rerun:
            continue
        inputs = []
        for source in stage["inputs"]:
            if source.get("kind") == "stage" and source["stage"] not in rerun:
                if status[source["stage"]] != "succeeded":
                    raise RetryRefused(
                        f"Stage {source['stage']!r} did not succeed and is not retried."
                    )
                if len(source["outputs"]) != 1:
                    raise RetryRefused(
                        f"Input {source['name']!r} reads several outputs of {source['stage']!r}; "
                        "pin them by hand so the mount layout stays yours to review."
                    )
                if (source["stage"], source["outputs"][0]) in unreleased:
                    raise RetryRefused(
                        f"Output {source['outputs'][0]!r} of {source['stage']!r} is not "
                        "custody-released and retained, so a later campaign cannot pin it; "
                        "a human restores custody first."
                    )
                found = digests.get((source["stage"], source["outputs"][0]), set())
                if len(found) != 1:
                    raise RetryRefused(
                        f"Output {source['outputs'][0]!r} of {source['stage']!r} has no single "
                        "recorded digest."
                    )
                sha256 = next(iter(found))
                inputs.append(
                    {
                        "kind": "artifact",
                        "name": source["name"],
                        "campaign_id": campaign["id"],
                        "stage": source["stage"],
                        "output": source["outputs"][0],
                        "sha256": sha256,
                    }
                )
                reused.append(
                    {"stage": source["stage"], "output": source["outputs"][0], "sha256": sha256}
                )
            else:
                inputs.append(source)
        stage["inputs"] = inputs
        kept.append(stage)
    original = int(spec["budget_cap_cents"])
    needed = sum(int(stage.get("max_exposure_cents", 0)) for stage in kept)
    default = max(original - int(campaign.get("settled_cents", 0)), needed)
    cap = default if budget_cap_cents is None else budget_cap_cents
    if cap > original:
        raise RetryRefused("A retry may not ask for more than the original campaign's cap.")
    if cap < needed:
        raise RetryRefused(f"The cap must cover the retried stages' exposure ({needed} cents).")
    spec["stages"] = kept
    spec["budget_cap_cents"] = cap
    spec["name"] = (spec["name"] + "-retry")[:MAX_NAME]
    return {
        "spec": spec,
        "source_campaign_id": campaign["id"],
        "retried_stages": [stage["key"] for stage in kept],
        "reused_outputs": reused,
        "note": (
            "A new spec for human approval; nothing was submitted. Validate it with "
            "tessera_validate_campaign_spec, then submit it with tessera_submit_campaign."
        ),
    }
